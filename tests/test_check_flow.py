#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Monitor.check() end to end against fake storefronts.

Origin: the steal branch kept a stale reference to a single `scraper` after
sources became per-rule. Every unit test passed and a local run was clean,
because that branch only executes when a steal candidate exists — the cloud
had twelve and crashed. These tests drive both branches with no network.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, sources  # noqa: E402
from watchmon.history import PriceHistory  # noqa: E402
from watchmon.models import Listing, Rule  # noqa: E402
from watchmon.notify import Notifier  # noqa: E402
from watchmon.runner import Monitor  # noqa: E402
from watchmon.scraper import ProductPage  # noqa: E402

BASE = "https://store-a.example.com"
SPEC = "Movement\nMechanical Automatic"


class FakeScraper:
    """Stands in for either source kind — same two methods."""

    def __init__(self, listings):
        self.listings = listings
        self.fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def sweep(self, query, max_pages, label=""):
        return list(self.listings)

    def fetch_product(self, listing):
        self.fetched.append(listing.pid)
        return ProductPage(
            pid=listing.pid, url=listing.url, title=listing.title, heading=listing.title,
            price=listing.price, movement="Mechanical Automatic",
            in_stock=True, stock_note="in stock", text=SPEC,
        )


def listing(pid, price):
    return Listing(pid=pid, url=f"{BASE}/x/p/{pid}", title="Acme Automatic Watch",
                   price=price, brand="acme")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """One fake storefront, one rule that matches it."""
    monkeypatch.setattr(config, "SITE_BASE", BASE)
    monkeypatch.setattr(config, "SITE_B_BASE", "")
    monkeypatch.setattr(config, "BRANDS", ("acme",))
    monkeypatch.setattr(config, "IGNORE_PATTERNS", ())
    rule = Rule(name="test rule", brands=("acme",),
                sources={"a": "acme"}, history_sources={"a": "acme"},
                include=r"automatic", ceiling=10000,
                require_spec={r"(type of )?movement": r"automatic"})
    monkeypatch.setattr(config, "RULES", (rule,))

    scraper = FakeScraper([listing("p1", 5000)])

    class FakeSource(sources.Source):
        """Source is frozen, so subclass rather than patch an attribute."""

        def open(self, headless: bool = True):
            return scraper

    source = FakeSource(key="a", kind="browser", max_pages=1)
    monkeypatch.setattr(sources, "enabled", lambda: [source])
    monkeypatch.setattr(sources, "SOURCES", (source,))

    monitor = Monitor(threshold=10000, history=PriceHistory(tmp_path / "h.db"),
                      notifier=Notifier(channels=[]))
    return monitor, scraper


def test_ceiling_branch_produces_a_deal(wired):
    monitor, scraper = wired
    deals = monitor.check(wide=False)
    assert [d.kind for d in deals] == ["under_threshold"]
    assert deals[0].price == 5000
    assert "p1" in scraper.fetched


def test_steal_branch_runs_without_a_stale_scraper(wired, monkeypatch):
    """The exact regression: a steal candidate that no ceiling rule swept."""
    monitor, scraper = wired
    now = datetime.now().timestamp()

    # Seed a baseline high enough that today's price is a steal, on a product
    # the ceiling rule will not pick up (priced above the ceiling).
    expensive = Listing(pid="p9", url=f"{BASE}/x/p/p9", title="Acme Automatic Watch",
                        price=50000, brand="acme")
    for offset in range(-config.STEAL_MIN_HISTORY_DAYS - 2, 0):
        day = (datetime.now() + timedelta(days=offset)).timestamp()
        monitor.history.record([expensive], day)

    scraper.listings = [Listing(pid="p9", url=f"{BASE}/x/p/p9",
                                title="Acme Automatic Watch", price=20000, brand="acme")]
    deals = monitor.check(wide=False)
    assert [d.kind for d in deals] == ["steal"], deals
    assert "p9" in scraper.fetched


def test_a_steal_from_an_unopened_source_is_skipped_not_crashed(wired, monkeypatch):
    monitor, scraper = wired
    stray = Listing(pid="zz", url="https://elsewhere.test/x", title="Acme Automatic", price=1)
    monkeypatch.setattr(
        Monitor, "_screen_steals",
        lambda self, tracked, now: [(stray, __import__("watchmon.models", fromlist=["PriceStats"]).PriceStats(days=99, median=100, min_ever=1), "r")],
    )
    monitor.check(wide=False)  # must not raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
