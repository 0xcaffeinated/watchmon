#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Price history: daily buckets, and the stats the steal rule depends on."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon.history import PriceHistory  # noqa: E402
from watchmon.models import Listing  # noqa: E402


def ts_for(day_offset: int) -> float:
    """Epoch seconds at noon, `day_offset` days from today (negative = past)."""
    base = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).timestamp()


def L(pid="p1", price=10000, brand="timex"):
    return Listing(pid=pid, url=f"https://f.com/x/p/{pid}", title="Timex Watch", price=price, brand=brand)


@pytest.fixture
def history(tmp_path):
    return PriceHistory(tmp_path / "h.db")


def test_records_and_reads_back_a_series(history):
    for offset, price in [(-3, 9000), (-2, 8000), (-1, 8500)]:
        history.record([L(price=price)], ts_for(offset))
    assert [p for _, p in history.series("p1")] == [9000, 8000, 8500]


def test_same_day_keeps_the_lower_price(history):
    """Two runs on one day must not become two data points, and an intraday
    dip must survive a bounce-back before the next run."""
    history.record([L(price=9000)], ts_for(0))
    history.record([L(price=7000)], ts_for(0))
    history.record([L(price=9500)], ts_for(0))
    assert history.series("p1") == [(_today(), 7000)]


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def test_unpriced_listings_are_not_recorded(history):
    assert history.record([Listing(pid="p1", url="u", title="t", price=None)], ts_for(0)) == 0
    assert history.series("p1") == []


def test_stats_exclude_today(history):
    """Today is excluded so a price recorded this run cannot become its own
    all-time low or drag its own median down."""
    for offset in (-3, -2, -1):
        history.record([L(price=10000)], ts_for(offset))
    history.record([L(price=5000)], ts_for(0))

    stats = history.stats("p1", ts_for(0), window_days=30)
    assert stats.days == 3
    assert stats.median == 10000
    assert stats.min_ever == 10000  # not 5000


def test_stats_median_uses_only_the_window(history):
    history.record([L(price=1000)], ts_for(-40))  # outside a 30-day window
    for offset in range(-5, 0):
        history.record([L(price=9000)], ts_for(offset))

    stats = history.stats("p1", ts_for(0), window_days=30)
    assert stats.median == 9000
    assert stats.min_ever == 1000  # all-time low still spans everything
    assert stats.days == 6


def test_stats_for_unknown_product_is_empty(history):
    stats = history.stats("nope", ts_for(0), 30)
    assert stats.days == 0 and stats.median is None and not stats.has_enough_history


def test_stats_many_matches_stats_one_by_one(history):
    for offset in range(-4, 0):
        history.record([L("a", 8000), L("b", 20000)], ts_for(offset))
    now = ts_for(0)
    bulk = history.stats_many(["a", "b", "missing"], now, 30)
    assert bulk["a"].median == history.stats("a", now, 30).median
    assert bulk["b"].median == history.stats("b", now, 30).median
    assert bulk["missing"].days == 0


def test_product_metadata_is_upserted(history):
    history.record([L(price=9000)], ts_for(-1))
    history.record([L(price=8000)], ts_for(0))
    product = history.product("p1")
    assert product["brand"] == "timex"
    assert product["first_seen"] < product["last_seen"]


def test_summary_counts_products_and_points(history):
    history.record([L("a", 100), L("b", 200)], ts_for(-1))
    history.record([L("a", 150)], ts_for(0))
    summary = history.summary()
    assert summary["products"] == 2
    assert summary["price_points"] == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
