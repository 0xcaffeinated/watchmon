"""Storefront access. The only module that talks to a browser.

Plain HTTP gets a 403, so everything goes through headless Chromium. The
origin comes from config, never a literal — see config.SITE_BASE.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import config, parsing
from .models import Listing

log = logging.getLogger("watchmon.scraper")

_CARD_JS = """(base) => {
    const seen = new Set();
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/p/"]')) {
        const href = a.getAttribute('href') || '';
        const parts = href.split('/p/');
        if (parts.length < 2) continue;
        const pid = parts[1].split('?')[0];
        if (seen.has(pid)) continue;
        seen.add(pid);
        const box = a.closest('div[class]')?.parentElement;
        const blob = ((box && box.innerText) || a.innerText || '').replace(/\\s+/g, ' ');
        out.push({
            pid,
            url: base + (href.startsWith('/') ? href : '/' + href).split('?')[0],
            title: (a.getAttribute('title') || '').trim() || blob.slice(0, 120),
            blob,
        });
    }
    return out;
}"""

_PRODUCT_JS = """() => ({
    ld: [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent),
    h1: (document.querySelector('h1') || {}).innerText || document.title,
    text: document.body.innerText || '',
})"""


@dataclass
class ProductPage:
    """What the product page says. JSON-LD is the authority on price."""

    pid: str
    url: str
    title: str
    heading: str
    price: int | None
    movement: str | None
    in_stock: bool
    stock_note: str
    # Raw page text, so a rule can read any specification it likes rather than
    # the scraper having to know which ones matter.
    text: str = ""


class StoreScraper:
    """Owns the browser. Use as a context manager."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._page = None

    def __enter__(self) -> StoreScraper:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = self._browser.new_context(
            user_agent=config.USER_AGENT,
            locale="en-IN",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = context.new_page()
        self._warm_up()
        return self

    def _warm_up(self) -> None:
        """Load the homepage once so the session has cookies before searching.

        Proven necessary from a datacenter IP: a cold session requesting
        /search hangs until timeout, while the same session succeeds after one
        homepage visit (200, 11 cookies, then 125 product links). Cheap enough
        to always do — it may also be behind the timeouts seen locally when the
        machine scrapes straight after waking.
        """
        try:
            response = self._page.goto(
                config.SITE_BASE + "/", wait_until="domcontentloaded", timeout=45_000
            )
            self._page.wait_for_timeout(2000)
            log.info("session warm-up: HTTP %s", response.status if response else "?")
        except Exception as exc:  # noqa: BLE001 - warm-up must never be fatal
            log.warning("session warm-up failed (%s) — continuing anyway", exc)

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    # -------------------------------------------------------------- search --

    def _cards(self) -> list[Listing]:
        raw = self._page.evaluate(_CARD_JS, config.SITE_BASE)
        return [
            Listing(
                pid=r["pid"],
                url=r["url"],
                title=r["title"],
                price=parsing.parse_price(r["blob"]),
            )
            for r in raw
        ]

    def sweep(self, query: str, max_pages: int, label: str = "") -> list[Listing]:
        """Page a search until results repeat or max_pages is reached.

        Never stops early on price: the site's price_asc is not monotonic
        across pages, so a page of expensive results does not mean the cheap
        ones are behind us.
        """
        collected: list[Listing] = []
        seen: set[str] = set()

        for page_no in range(1, max_pages + 1):
            url = config.SITE_BASE + config.SEARCH_PATH.format(query=query, page=page_no)
            listings, reached_end = self._load_page(url, label, page_no)
            if not listings:
                if not reached_end:
                    log.warning(
                        "%s page %d: still empty after %d retry(ies) — ending sweep",
                        label,
                        page_no,
                        config.PAGE_RETRY_ATTEMPTS,
                    )
                break

            fresh = parsing.new_pids(listings, seen)
            if not fresh:
                log.info("%s page %d repeated earlier results — end of listings", label, page_no)
                break
            seen |= fresh
            collected.extend(x for x in listings if x.pid in fresh)

            total = self._pager_total()
            if total is not None and page_no >= total:
                log.info("%s: reached the last page (%d of %d)", label, page_no, total)
                break
            time.sleep(config.PAGE_PAUSE_SEC)

        return collected

    def _pager_total(self) -> int | None:
        try:
            text = self._page.evaluate("() => document.body.innerText || ''")
        except Exception:  # noqa: BLE001 - pager is a nicety, never fatal
            return None
        return parsing.parse_pager_total(text)

    def _load_page(self, url: str, label: str, page_no: int) -> tuple[list[Listing], bool]:
        """Fetch one search page. Returns (listings, reached_end_cleanly).

        An empty page has two very different causes, and conflating them made
        every completed sweep look like a failure:

          * the site throttled this request — worth a retry and a warning;
          * it is the last page, which the site often serves empty and with a
            nonsense range ("Showing 81 – 80 of 102" for alba+automatic, where
            page 2 ended at 73). That is a normal end of results.
        """
        for attempt in range(config.PAGE_RETRY_ATTEMPTS + 1):
            self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # Wait for a product link rather than a fixed sleep. The last page
            # of a result set holds only a handful of cards and renders later
            # than a full one, so a flat 2.5s sampled it while still empty and
            # the sweep concluded the results had ended.
            try:
                self._page.wait_for_selector(
                    'a[href*="/p/"]', timeout=config.CARD_WAIT_MS, state="attached"
                )
            except Exception:  # noqa: BLE001 - genuinely empty pages hit this
                pass
            self._page.wait_for_timeout(600)
            listings = self._cards()
            if listings:
                if attempt:
                    log.info("%s page %d recovered on retry %d", label, page_no, attempt)
                return listings, False

            total = self._pager_total()
            if total is not None and page_no >= total:
                log.info(
                    "%s: page %d of %d is empty — end of results, sweep complete",
                    label,
                    page_no,
                    total,
                )
                return [], True

            if attempt < config.PAGE_RETRY_ATTEMPTS:
                log.info(
                    "%s page %d came back empty — backing off %.1fs and retrying",
                    label,
                    page_no,
                    config.PAGE_RETRY_DELAY_SEC,
                )
                time.sleep(config.PAGE_RETRY_DELAY_SEC)
        return [], False

    # ------------------------------------------------------------- product --

    def fetch_product(self, listing: Listing) -> ProductPage | None:
        """Open a product page and read the authoritative fields."""
        self._page.goto(listing.url, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(1500)
        data = self._page.evaluate(_PRODUCT_JS)

        product = parsing.extract_ld_product(data.get("ld") or [])
        if product is None:
            log.warning("%s: no JSON-LD product data (layout change?)", listing.pid)
            return None

        text = data.get("text") or ""
        heading = parsing.clean_title(data.get("h1") or "")
        in_stock, stock_note = parsing.determine_stock(text, product["in_stock"])

        return ProductPage(
            pid=listing.pid,
            url=listing.url,
            # The JSON-LD name drops the model code on some listings while the
            # h1 keeps it; both are carried so ignore-matching can see either.
            title=product["name"] or heading or listing.title,
            heading=heading,
            price=product["price"],
            movement=parsing.extract_movement(text),
            in_stock=in_stock,
            stock_note=stock_note,
            text=text,
        )
