"""Orchestration: one check, start to finish.

Two detectors share a single browser session and a single history write:

  * the ₹8,000 automatic rule — unchanged behaviour, narrow brand+automatic
    sweep, product page confirms every hit;
  * steal detection — wide sweep over every watch of the brands, card prices
    feed history, and only products that clear the historical bar get a page
    fetch.
"""

from __future__ import annotations

import json
import logging
import time

from . import config, parsing, steals
from .history import PriceHistory
from .models import Deal, Listing, PriceStats, now_iso
from .notify import Notifier
from .runstate import (
    classify_wake,
    clear_backoff,
    effective_min_interval,
    lid_closed,
    load_state,
    record_attempt,
    record_failure,
    record_transient_failure,
    record_success,
    save_state,
    seconds_since_user_input,
    seconds_since_wake,
    should_run,
    single_instance,
    wait_for_network,
)
from .scraper import StoreScraper, ProductPage

log = logging.getLogger("watchmon.runner")


def decide_alerts(deals: list[Deal], state: dict) -> tuple[list[Deal], dict]:
    """Alert on a new deal, or one that got cheaper since we last said so.

    Re-arms automatically: a deal that stops qualifying drops out of the map,
    so the next time it qualifies it alerts again.
    """
    alerted = state.get("alerted", {})
    to_alert = [d for d in deals if alerted.get(d.alert_key) is None or d.price < alerted[d.alert_key]]

    stale = set(alerted) - {d.alert_key for d in deals}
    if stale:
        log.info("re-armed: %s", ", ".join(sorted(stale)))

    merged = {**state, "alerted": {d.alert_key: d.price for d in deals}, "updated": now_iso()}
    return to_alert, merged


def record_deals(deals: list[Deal]) -> None:
    config.DEALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.DEALS_FILE.open("a") as fh:
        for deal in deals:
            fh.write(json.dumps(deal.to_dict()) + "\n")


def _in_stock_or_dropped(page: ProductPage, kind: str) -> bool:
    """False when a listing should be dropped for being unavailable.

    Logged at WARNING rather than INFO: this is the one filter that can hide a
    genuinely good price, so it must be easy to find when something expected
    fails to arrive.
    """
    if page.in_stock or config.ALERT_ON_OUT_OF_STOCK:
        return True
    log.warning(
        "DROPPED %s ₹%s — %s (%s): %s",
        kind,
        page.price,
        page.stock_note,
        page.title[:45],
        page.url,
    )
    return False


def _confirm_automatic(page: ProductPage, listing: Listing, threshold: int) -> Deal | None:
    """Apply the ₹8,000 automatic rule to a fetched product page."""
    if page.price is None:
        log.warning("skip %s: JSON-LD carried no price", page.pid)
        return None

    # Re-checked here because search cards usually lack the model code, and on
    # some listings only the h1 carries it.
    if parsing.is_ignored(f"{page.title} {page.heading}", page.url):
        log.info("skip %s: ignored model at ₹%s", page.pid, page.price)
        return None

    if page.price >= threshold:
        log.info("skip %s: ₹%s (card said ₹%s)", page.pid, page.price, listing.price)
        return None

    automatic, movement_note = parsing.is_automatic_movement(page.movement, page.title)
    if not automatic:
        log.info("skip %s: %s", page.pid, movement_note)
        return None

    if not _in_stock_or_dropped(page, "under_threshold"):
        return None

    return Deal(
        pid=page.pid,
        url=page.url,
        brand=parsing.brand_of(page.title, page.url) or listing.brand or "?",
        title=page.title,
        price=page.price,
        kind="under_threshold",
        reason=f"below ₹{threshold:,}",
        movement=page.movement or "(not listed)",
        in_stock=page.in_stock,
        stock_note=page.stock_note,
    )


def _confirm_steal(page: ProductPage, stats: PriceStats, card_reason: str) -> Deal | None:
    """Re-run the steal test against the authoritative page price."""
    if page.price is None:
        log.warning("skip steal %s: no price on product page", page.pid)
        return None

    if parsing.is_ignored(f"{page.title} {page.heading}", page.url):
        log.info("skip steal %s: ignored model", page.pid)
        return None

    confirmed, reason = steals.is_steal(page.price, stats)
    if not confirmed:
        log.info("steal %s not confirmed on product page: %s", page.pid, reason)
        return None

    if not _in_stock_or_dropped(page, "steal"):
        return None

    return Deal(
        pid=page.pid,
        url=page.url,
        brand=parsing.brand_of(page.title, page.url) or "?",
        title=page.title,
        price=page.price,
        kind="steal",
        reason=reason,
        movement=page.movement or "(not listed)",
        in_stock=page.in_stock,
        stock_note=page.stock_note,
        stats=stats,
    )


class Monitor:
    """One check. Construct, call run(), read the result."""

    def __init__(
        self,
        threshold: int = config.THRESHOLD_INR,
        headless: bool = True,
        history: PriceHistory | None = None,
        notifier: Notifier | None = None,
    ):
        self.threshold = threshold
        self.headless = headless
        self.history = history if history is not None else PriceHistory(config.HISTORY_DB)
        self.notifier = notifier if notifier is not None else Notifier()

    # ------------------------------------------------------------ phases ---

    def _sweep_all(
        self, scraper: StoreScraper, wide: bool
    ) -> tuple[list[Listing], list[Listing]]:
        """Sweeps sharing one browser. Returns (automatic candidates, tracked).

        `wide` adds the every-watch-of-the-brand pass that feeds history. It is
        skipped on most ticks: see config.HISTORY_INTERVAL_SEC.
        """
        automatic_candidates: list[Listing] = []
        all_watched: dict[str, Listing] = {}

        for brand in config.BRANDS:
            listings = scraper.sweep(
                config.AUTOMATIC_QUERY.format(brand=brand),
                config.MAX_PAGES_AUTOMATIC,
                label=f"{brand}/auto",
            )
            found = parsing.select_candidates(listings, self.threshold)
            automatic_candidates.extend(found)
            for item in parsing.watched_listings(listings):
                all_watched.setdefault(item.pid, item)
            log.info("%s/auto: %d listings, %d candidate(s)", brand, len(listings), len(found))

        if wide:
            for brand in config.BRANDS:
                listings = scraper.sweep(
                    config.HISTORY_QUERY.format(brand=brand),
                    config.MAX_PAGES_HISTORY,
                    label=f"{brand}/all",
                )
                watched = parsing.watched_listings(listings)
                for item in watched:
                    all_watched.setdefault(item.pid, item)
                log.info("%s/all: %d listings, %d watched", brand, len(listings), len(watched))
        else:
            log.info("wide history sweep not due — tracking %d from the automatic sweep", len(all_watched))

        return automatic_candidates, list(all_watched.values())

    def _screen_steals(self, tracked: list[Listing], now: float) -> list[tuple[Listing, PriceStats, str]]:
        """Which tracked products look like steals on their card price."""
        priced = {x.pid: x.price for x in tracked if x.price is not None}
        stats = self.history.stats_many(
            list(priced), now, config.STEAL_MEDIAN_WINDOW_DAYS
        )
        by_pid = {x.pid: x for x in tracked}
        return [
            (by_pid[pid], stats.get(pid, PriceStats()), reason)
            for pid, reason in steals.find_steals(priced, stats)
        ]

    # --------------------------------------------------------------- run ---

    def check(self, wide: bool = True) -> list[Deal]:
        """Scrape, record history, and return every qualifying deal."""
        now = time.time()
        deals: list[Deal] = []

        with StoreScraper(headless=self.headless) as scraper:
            automatic_candidates, tracked = self._sweep_all(scraper, wide=wide)

            # Steal screening runs against history *before* today's prices are
            # written, so a product cannot be compared against itself.
            steal_candidates = self._screen_steals(tracked, now)
            recorded = self.history.record(tracked, now)
            log.info(
                "history: %d product(s) recorded, %d steal candidate(s)",
                recorded,
                len(steal_candidates),
            )

            unique = {c.pid: c for c in automatic_candidates}
            ordered = sorted(unique.values(), key=lambda x: x.price if x.price is not None else 0)
            if len(ordered) > config.MAX_VERIFY_PER_RUN:
                log.warning(
                    "%d automatic candidates exceed the %d/run cap — NOT checking %d "
                    "(cheapest are checked first)",
                    len(ordered),
                    config.MAX_VERIFY_PER_RUN,
                    len(ordered) - config.MAX_VERIFY_PER_RUN,
                )
                ordered = ordered[: config.MAX_VERIFY_PER_RUN]

            for listing in ordered:
                page = scraper.fetch_product(listing)
                if page is None:
                    continue
                deal = _confirm_automatic(page, listing, self.threshold)
                if deal:
                    deals.append(deal)
                time.sleep(0.8)

            already = {d.pid for d in deals}
            for listing, stats, _reason in steal_candidates:
                if listing.pid in already:
                    continue  # already alerting on the ₹8,000 rule
                page = scraper.fetch_product(listing)
                if page is None:
                    continue
                deal = _confirm_steal(page, stats, _reason)
                if deal:
                    deals.append(deal)
                time.sleep(0.8)

        return deals

    @staticmethod
    def _note_failure(state: dict, now: float, reason: str, dark_wake: bool) -> dict:
        """Escalate a real failure; hold a dark-wake one harmless."""
        if dark_wake:
            log.info("failure during a dark wake — retrying in %ds, not escalating", config.WAKE_RETRY_SEC)
            return record_transient_failure(state, now, reason)
        return record_failure(state, now, reason)

    def run(self, force: bool = False, dry_run: bool = False) -> int:
        """Guarded run: lock, throttle, network, scrape, alert."""
        with single_instance() as acquired:
            if not acquired:
                log.info("another check is still running — skipping this tick")
                return 0
            return self._guarded(force=force, dry_run=dry_run)

    def _guarded(self, force: bool, dry_run: bool) -> int:
        now = time.time()
        state = load_state()
        force = force or dry_run

        shut = lid_closed()
        just_woke, user_present = classify_wake(
            seconds_since_wake(now), seconds_since_user_input()
        )

        # Being present should never be met with a backoff earned while the
        # machine was asleep failing dark-wake scrapes.
        if user_present and (state.get("backoff_until_ts") or 0) > now:
            log.info("user is present — clearing a %ds backoff", int(state["backoff_until_ts"] - now))
            state = clear_backoff(state)
            save_state(state)

        min_interval = effective_min_interval(config.MIN_INTERVAL_SEC, shut)
        go, why = should_run(now, state, min_interval, force=force)
        if not go:
            log.info("skipping: %s%s", why, " (lid closed)" if shut else "")
            return 0
        if shut:
            log.info("lid is closed — checking at %ds intervals", min_interval)

        if not dry_run:
            state = record_attempt(state, now)
            save_state(state)

        usable, net_reason = wait_for_network()
        if not usable:
            if not dry_run:
                state = self._note_failure(
                    state, now, f"network {net_reason}", just_woke and not user_present
                )
                save_state(state)
            log.info("no usable network (%s)", net_reason)
            return 0

        wide = (now - (state.get("last_history_ts") or 0)) >= config.HISTORY_INTERVAL_SEC
        log.info(
            "checking %s under ₹%s + steals (network %s, history sweep: %s)",
            "/".join(b.title() for b in config.BRANDS),
            self.threshold,
            net_reason,
            "yes" if wide else "not due",
        )
        try:
            deals = self.check(wide=wide)
        except Exception as exc:  # noqa: BLE001 - a scheduled job must not die silently
            log.exception("scrape failed: %s", exc)
            if not dry_run:
                save_state(
                    self._note_failure(
                        state, now, f"scrape: {exc}", just_woke and not user_present
                    )
                )
            return 1

        for deal in deals:
            log.info("%s: ₹%s %s — %s", deal.kind, deal.price, deal.title[:60], deal.reason)
        if not deals:
            log.info("nothing qualifying right now")

        if dry_run:
            print(json.dumps([d.to_dict() for d in deals], indent=2, ensure_ascii=False))
            return 0

        state = record_success(state, now)
        if wide:
            state["last_history_ts"] = now
        to_alert, state = decide_alerts(deals, state)
        if to_alert:
            record_deals(to_alert)
            self.notifier.announce(to_alert, self.threshold)
        else:
            log.info("nothing new to announce")
        save_state(state)
        return 0
