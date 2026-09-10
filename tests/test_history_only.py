#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""History-only rules: sweep broadly, never notify.

The database wants depth across the whole catalogue; the phone wants watches
only. These pin that separation, because the failure mode — a category rule
quietly producing steal alerts — is exactly the noise the design exists to
prevent.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, parsing, sources  # noqa: E402
from watchmon.history import PriceHistory  # noqa: E402
from watchmon.models import Listing, Rule  # noqa: E402
from watchmon.notify import Notifier  # noqa: E402
from watchmon.runner import Monitor  # noqa: E402
from watchmon.scraper import ProductPage  # noqa: E402

BASE = "https://store-a.example.com"

ALERTING = Rule(
    name="watches", brands=("acme",),
    sources={"a": "acme+automatic"}, history_sources={"a": "acme+watch"},
    include=r"automatic", ceiling=10000,
)
HISTORY_ONLY = Rule(
    name="history: headphones", brands=(),
    history_sources={"a": "headphones"}, alerts=False, max_pages=4,
)


class FakeScraper:
    def __init__(self, by_query):
        self.by_query = by_query
        self.pages_asked = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def sweep(self, query, max_pages, label=""):
        self.pages_asked[query] = max_pages
        return list(self.by_query.get(query, []))

    def fetch_product(self, listing):
        return ProductPage(
            pid=listing.pid, url=listing.url, title=listing.title, heading=listing.title,
            price=listing.price, movement="Mechanical Automatic", in_stock=True,
            stock_note="in stock", text="Movement\nMechanical Automatic",
        )


def L(pid, title, price):
    return Listing(pid=pid, url=f"{BASE}/x/p/{pid}", title=title, price=price)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SITE_BASE", BASE)
    monkeypatch.setattr(config, "SITE_B_BASE", "")
    monkeypatch.setattr(config, "BRANDS", ("acme",))
    monkeypatch.setattr(config, "IGNORE_PATTERNS", ())
    monkeypatch.setattr(config, "RULES", (ALERTING, HISTORY_ONLY))

    scraper = FakeScraper({
        "acme+automatic": [L("w1", "Acme Automatic Watch", 5000)],
        "acme+watch": [L("w1", "Acme Automatic Watch", 5000),
                       L("w2", "Acme Quartz Watch", 3000)],
        "headphones": [L("h1", "Sonic Headphones", 999),
                       L("h2", "Boomy Headphones", 4500)],
    })

    class FakeSource(sources.Source):
        def open(self, headless: bool = True):
            return scraper

    source = FakeSource(key="a", kind="browser", max_pages=6)
    monkeypatch.setattr(sources, "enabled", lambda: [source])
    monkeypatch.setattr(sources, "SOURCES", (source,))
    monitor = Monitor(threshold=10000, history=PriceHistory(tmp_path / "h.db"),
                      notifier=Notifier(channels=[]))
    return monitor, scraper


def test_history_only_rules_are_recorded(wired):
    monitor, _ = wired
    monitor.check(wide=True)
    summary = monitor.history.summary()
    assert summary["products"] >= 4  # both watches and both headphones
    assert monitor.history.product("h1") is not None


def test_history_only_rules_never_alert(wired):
    monitor, _ = wired
    deals = monitor.check(wide=True)
    assert all(d.rule != HISTORY_ONLY.name for d in deals)
    assert all("headphone" not in d.title.lower() for d in deals)


def test_a_cheap_history_only_product_does_not_become_a_steal(wired):
    """The failure this design prevents: a broad category sweep firing steals."""
    monitor, scraper = wired
    for offset in range(-config.STEAL_MIN_HISTORY_DAYS - 2, 0):
        day = (datetime.now() + timedelta(days=offset)).timestamp()
        monitor.history.record([L("h2", "Boomy Headphones", 40000)], day)

    scraper.by_query["headphones"] = [L("h2", "Boomy Headphones", 9000)]
    deals = monitor.check(wide=True)
    assert [d for d in deals if d.kind == "steal"] == []


def test_an_alerting_product_still_steals(wired):
    """Same setup, but on the rule that does alert — proves the filter is
    scoped to history-only rules and has not muted everything."""
    monitor, scraper = wired
    for offset in range(-config.STEAL_MIN_HISTORY_DAYS - 2, 0):
        day = (datetime.now() + timedelta(days=offset)).timestamp()
        monitor.history.record([L("w2", "Acme Quartz Watch", 40000)], day)

    scraper.by_query["acme+watch"] = [L("w2", "Acme Quartz Watch", 9000)]
    deals = monitor.check(wide=True)
    assert any(d.kind == "steal" for d in deals), deals


def test_history_only_rules_are_not_swept_narrowly(wired):
    monitor, scraper = wired
    monitor.check(wide=False)
    assert "headphones" not in scraper.pages_asked


def test_history_rules_use_their_own_page_budget(wired):
    monitor, scraper = wired
    monitor.check(wide=True)
    assert scraper.pages_asked["headphones"] == 4
    assert scraper.pages_asked["acme+watch"] == config.MAX_PAGES_HISTORY


# --------------------------------------------------------------- filters ----


def test_a_category_rule_keeps_everything_it_finds():
    kept = parsing.tracked_by([L("h1", "Sonic Headphones", 999)], HISTORY_ONLY)
    assert [x.pid for x in kept] == ["h1"]


def test_a_brand_rule_keeps_only_its_brands(monkeypatch):
    monkeypatch.setattr(config, "BRANDS", ("acme",))
    monkeypatch.setattr(config, "IGNORE_PATTERNS", ())
    listings = [L("a", "Acme Quartz Watch", 100), L("b", "Rival Quartz Watch", 100)]
    assert [x.pid for x in parsing.tracked_by(listings, ALERTING)] == ["a"]


def test_the_wide_filter_ignores_the_narrow_include(monkeypatch):
    """History wants the whole brand catalogue, not just what could alert —
    otherwise a watch's median is built from automatics alone."""
    monkeypatch.setattr(config, "BRANDS", ("acme",))
    monkeypatch.setattr(config, "IGNORE_PATTERNS", ())
    quartz = L("q", "Acme Quartz Watch", 100)
    assert parsing.matches_rule(quartz.title, quartz.url, ALERTING) is False
    assert [x.pid for x in parsing.tracked_by([quartz], ALERTING)] == ["q"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
