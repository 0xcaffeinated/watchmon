"""A storefront that embeds its whole catalogue as JSON in the page.

The first storefront renders prices in the DOM and blocks plain HTTP, so it
needs a browser. This one is the mirror image: headless Chromium is refused at
the protocol level while a plain request succeeds, and every search page ships
a JSON blob with name, brand, price, MRP and stock already structured.

That makes this source cheaper and more reliable than scraping rendered HTML —
no browser, no carousel ambiguity, no price guessing.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import config
from .models import Listing
from .scraper import ProductPage

log = logging.getLogger("watchmon.jsonstore")

STATE_RE = re.compile(r"window\.__myx\s*=\s*(\{.*?\});?\s*</script>", re.S)


def _walk(node, key: str):
    """Yield every value stored under `key`, at any depth."""
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _walk(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, key)


def extract_state(html: str) -> dict | None:
    """Pull the embedded catalogue JSON out of a page."""
    match = STATE_RE.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def products_in(state: dict) -> list[dict]:
    """Every product node in the state blob.

    Identified structurally (a name plus a price) rather than by its position,
    which shifts between page types.
    """
    found: list[dict] = []

    def scan(node):
        if isinstance(node, dict):
            if "productName" in node and "price" in node:
                found.append(node)
            for value in node.values():
                scan(value)
        elif isinstance(node, list):
            for value in node:
                scan(value)

    scan(state)
    return found


def to_listing(node: dict, base: str) -> Listing | None:
    """One catalogue entry -> a Listing, or None if it is unusable."""
    pid = node.get("productId")
    price = node.get("price")
    if pid is None or not isinstance(price, int):
        return None
    slug = (node.get("landingPageUrl") or "").lstrip("/")
    brand = node.get("brand") or ""
    name = node.get("productName") or ""
    return Listing(
        pid=str(pid),
        url=f"{base}/{slug}" if slug else base,
        # Brand is a separate field here; fold it in so the shared brand and
        # rule matching, which read the title, behave as they do elsewhere.
        title=f"{brand} {name}".strip(),
        price=price,
        brand=brand.lower() or None,
    )


def in_stock_of(node: dict) -> bool:
    """True when any size/variant of this product is purchasable."""
    inventory = node.get("inventoryInfo")
    if isinstance(inventory, list) and inventory:
        return any(bool(v.get("available")) for v in inventory if isinstance(v, dict))
    return True


def specs_as_text(attributes: dict | None) -> str:
    """Render the attribute dict as the label/value lines specs parsing expects.

    Converting rather than special-casing means `parsing.extract_spec` and every
    rule's require_spec/reject_spec work here untouched.
    """
    if not isinstance(attributes, dict):
        return ""
    lines = []
    for label, value in attributes.items():
        if value in (None, "", "NA"):
            continue
        lines.append(str(label))
        lines.append(str(value))
    return "\n".join(lines)


class JsonStoreSource:
    """Same shape as the browser scraper, but over plain HTTP."""

    def __init__(self, base: str | None = None, headless: bool = True):
        self.base = (base or config.SITE_B_BASE).rstrip("/")
        self._last_request = 0.0

    # Context-manager parity with the browser source, so the runner does not
    # need to care which one it is holding.
    def __enter__(self) -> JsonStoreSource:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def _get(self, url: str) -> str | None:
        # Space out requests; this source is fast enough to be rude by accident.
        wait = config.JSONSTORE_PAUSE_SEC - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=config.JSONSTORE_TIMEOUT_SEC) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("fetch failed for %s: %s", url.split("?")[0], exc)
            return None
        finally:
            self._last_request = time.monotonic()

    def sweep(self, query: str, max_pages: int, label: str = "") -> list[Listing]:
        """Page a search until it stops returning products we have not seen."""
        collected: list[Listing] = []
        seen: set[str] = set()

        for page_no in range(1, max_pages + 1):
            url = self.base + config.JSONSTORE_SEARCH_PATH.format(query=query, page=page_no)
            html = self._get(url)
            if html is None:
                break
            state = extract_state(html)
            if state is None:
                log.warning("%s page %d: no catalogue JSON in the page", label, page_no)
                break

            listings = [x for x in (to_listing(n, self.base) for n in products_in(state)) if x]
            fresh = [x for x in listings if x.pid not in seen]
            if not fresh:
                log.info("%s: page %d added nothing new — end of results", label, page_no)
                break
            seen.update(x.pid for x in fresh)
            collected.extend(fresh)

        return collected

    def fetch_product(self, listing: Listing) -> ProductPage | None:
        """Read the authoritative price, stock and specifications."""
        html = self._get(listing.url)
        if html is None:
            return None
        state = extract_state(html)
        if state is None:
            log.warning("%s: no catalogue JSON on the product page", listing.pid)
            return None

        price = None
        for node in _walk(state, "price"):
            if isinstance(node, dict) and isinstance(node.get("discounted"), int):
                price = node["discounted"]
                break
        if price is None:
            price = listing.price

        attributes = next(iter(_walk(state, "articleAttributes")), None)
        text = specs_as_text(attributes)
        available = any(bool(v) for v in _walk(state, "available")) if state else True

        return ProductPage(
            pid=listing.pid,
            url=listing.url,
            title=listing.title,
            heading=listing.title,
            price=price,
            movement=None,
            in_stock=available,
            stock_note="in stock" if available else "no purchasable variant",
            text=text,
        )
