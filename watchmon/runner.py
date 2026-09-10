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

from contextlib import ExitStack

from . import config, parsing, sources, steals
from .history import PriceHistory
from .models import Deal, Listing, PriceStats, Rule, now_iso
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
from .scraper import ProductPage

log = logging.getLogger("watchmon.runner")


def rotate_history_rules(rules: list[Rule], offset: int, per_run: int) -> tuple[list[Rule], int]:
    """The slice of history-only rules this run should sweep, and the next offset.

    Prices are bucketed by calendar day, so a product needs to be seen once a
    day, not every run. Rotating a slice per run therefore buys breadth for
    free: the whole catalogue is covered across a day without any single run
    exceeding the job timeout or hammering the storefront.
    """
    if not rules:
        return [], 0
    if per_run >= len(rules):
        return list(rules), 0
    offset %= len(rules)
    window = rules[offset : offset + per_run]
    if len(window) < per_run:  # wrap around the end
        window += rules[: per_run - len(window)]
    return window, (offset + per_run) % len(rules)


def _source_key_for(listing: Listing) -> str:
    """Which storefront a listing came from, inferred from its URL."""
    for source in sources.SOURCES:
        if source.owns(listing.url):
            return source.key
    return sources.LEGACY_KEY


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


def _confirm_ceiling(page: ProductPage, listing: Listing, rule: Rule, threshold: int) -> Deal | None:
    """Apply a rule's price ceiling to a fetched product page."""
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

    ok, spec_note = parsing.check_specs(page.text, page.title, rule)
    if not ok:
        log.info("skip %s: %s", page.pid, spec_note)
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
        rule=rule.name,
        reason=f"below ₹{threshold:,}",
        spec=page.movement or "(not listed)",
        in_stock=page.in_stock,
        stock_note=page.stock_note,
    )


def _confirm_automatic(page: ProductPage, listing: Listing, threshold: int) -> Deal | None:
    """Compatibility shim: the watch rule, which is RULES[0]."""
    return _confirm_ceiling(page, listing, config.RULES[0], threshold)


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
        spec=page.movement or "(not listed)",
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
        # Where the history rotation resumes; carried in state between runs.
        self.history_offset = 0
        self.next_history_offset = 0

    # ------------------------------------------------------------ phases ---

    def _sweep_all(
        self, open_sources: dict, wide: bool
    ) -> tuple[list[tuple[Listing, Rule, str]], list[Listing]]:
        """Sweep every (rule, source) pair. Returns (candidates, tracked).

        Rules and storefronts vary independently: a rule declares a query for
        each source it applies to, and a source it says nothing about is not
        swept for it.
        """
        candidates: list[tuple[Listing, Rule, str]] = []
        tracked: dict[str, Listing] = {}

        for rule in config.RULES:
            if not rule.alerts:
                continue  # history-only: swept below, never a candidate
            ceiling = rule.ceiling if rule.ceiling is not None else self.threshold
            for key, scraper in open_sources.items():
                source = sources.by_key(key)
                for query in rule.queries_for(key):
                    pages = rule.max_pages or source.max_pages
                    listings = scraper.sweep(query, pages, label=f"{key}/{query}")
                    found = 0
                    for item in listings:
                        if not parsing.matches_rule(item.title, item.url, rule):
                            continue
                        item.brand = item.brand or parsing.brand_of(item.title, item.url)
                        item.rule = rule.name
                        tracked.setdefault(item.pid, item)
                        if item.price is None or item.price <= ceiling * config.VERIFY_MARGIN:
                            candidates.append((item, rule, key))
                            found += 1
                    log.info("%s/%s: %d listings, %d candidate(s)", key, query, len(listings), found)

        if wide:
            for rule in self._wide_rules():
                for key, scraper in open_sources.items():
                    source = sources.by_key(key)
                    pages = rule.max_pages or (
                        config.MAX_PAGES_HISTORY if key == "a" else source.max_pages
                    )
                    for query in rule.queries_for(key, wide=True):
                        listings = scraper.sweep(query, pages, label=f"{key}/{query}")
                        kept = parsing.tracked_by(listings, rule)
                        for item in kept:
                            item.rule = item.rule or rule.name
                            tracked.setdefault(item.pid, item)
                        log.info("%s/%s: %d listings, %d tracked", key, query, len(listings), len(kept))
        else:
            log.info(
                "wide history sweep not due — tracking %d from the rule sweeps", len(tracked)
            )

        return candidates, list(tracked.values())

    def _wide_rules(self) -> list[Rule]:
        """Alerting rules every wide run, plus this run's history slice."""
        alerting = [r for r in config.RULES if r.alerts]
        history = [r for r in config.RULES if not r.alerts]
        window, self.next_history_offset = rotate_history_rules(
            history, self.history_offset, config.HISTORY_RULES_PER_RUN
        )
        if history:
            log.info(
                "history rotation: %d of %d categories this run (offset %d -> %d)",
                len(window), len(history), self.history_offset, self.next_history_offset,
            )
        return alerting + window

    def _screen_steals(self, tracked: list[Listing], now: float) -> list[tuple[Listing, PriceStats, str]]:
        """Which tracked products look like steals on their card price.

        Restricted to rules that alert. Everything else is swept purely to give
        the database depth, and must never reach the phone.
        """
        alerting = {r.name for r in config.RULES if r.alerts}
        tracked = [x for x in tracked if x.rule is None or x.rule in alerting]
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

        with ExitStack() as stack:
            open_sources = {
                src.key: stack.enter_context(src.open(headless=self.headless))
                for src in sources.enabled()
            }
            log.info("storefronts: %s", ", ".join(sorted(open_sources)))
            ceiling_candidates, tracked = self._sweep_all(open_sources, wide=wide)

            # Steal screening runs against history *before* today's prices are
            # written, so a product cannot be compared against itself.
            steal_candidates = self._screen_steals(tracked, now)
            recorded = self.history.record(tracked, now)
            log.info(
                "history: %d product(s) recorded, %d steal candidate(s)",
                recorded,
                len(steal_candidates),
            )

            unique = {c.pid: (c, r, k) for c, r, k in ceiling_candidates}
            ordered = sorted(
                unique.values(), key=lambda t: t[0].price if t[0].price is not None else 0
            )
            if len(ordered) > config.MAX_VERIFY_PER_RUN:
                log.warning(
                    "%d candidates exceed the %d/run cap — NOT checking %d "
                    "(cheapest are checked first)",
                    len(ordered),
                    config.MAX_VERIFY_PER_RUN,
                    len(ordered) - config.MAX_VERIFY_PER_RUN,
                )
                ordered = ordered[: config.MAX_VERIFY_PER_RUN]

            for listing, rule, key in ordered:
                page = open_sources[key].fetch_product(listing)
                if page is None:
                    continue
                ceiling = rule.ceiling if rule.ceiling is not None else self.threshold
                deal = _confirm_ceiling(page, listing, rule, ceiling)
                if deal:
                    deals.append(deal)
                time.sleep(0.8)

            already = {d.pid for d in deals}
            source_of = {c.pid: k for c, _r, k in ceiling_candidates}
            for listing, stats, _reason in steal_candidates:
                if listing.pid in already:
                    continue  # already alerting on the ceiling rule
                # A steal can come from a product no ceiling rule swept, so the
                # storefront is inferred from its URL when it is not known.
                key = source_of.get(listing.pid) or _source_key_for(listing)
                scraper = open_sources.get(key)
                if scraper is None:
                    log.warning("steal %s: no open source for %r", listing.pid, key)
                    continue
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
        self.history_offset = int(state.get("history_offset") or 0)
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
            state["history_offset"] = self.next_history_offset
        to_alert, state = decide_alerts(deals, state)
        if to_alert:
            record_deals(to_alert)
            self.notifier.announce(to_alert, self.threshold)
        else:
            log.info("nothing new to announce")
        save_state(state)
        return 0
