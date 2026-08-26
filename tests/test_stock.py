#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Out-of-stock listings are dropped, not alerted (changed 17 Aug).

This reverses the earlier "only ever label" policy, so the risk it accepts is
pinned here: a false "out of stock" now costs a real deal outright. The drop is
logged at WARNING and counted in the daily report to keep that visible.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config  # noqa: E402
from watchmon.models import Listing, PriceStats  # noqa: E402
from watchmon.runner import _confirm_automatic, _confirm_steal, _in_stock_or_dropped  # noqa: E402
from watchmon.scraper import ProductPage  # noqa: E402


def page(price=7000, in_stock=True, note="in stock", title="TIMEX Automatic Watch"):
    return ProductPage(
        pid="p1", url="https://f.com/x/p/p1", title=title, heading=title, price=price,
        movement="Mechanical Automatic", in_stock=in_stock, stock_note=note,
    )


def listing():
    return Listing(pid="p1", url="https://f.com/x/p/p1", title="Timex", price=7000)


def stats():
    return PriceStats(days=config.STEAL_MIN_HISTORY_DAYS + 5, median=10000, min_ever=4600)


def test_in_stock_listing_passes():
    assert _in_stock_or_dropped(page(), "under_threshold") is True


def test_out_of_stock_listing_is_dropped():
    assert _in_stock_or_dropped(page(in_stock=False, note="selected variant out of stock"),
                                "under_threshold") is False


def test_threshold_alert_suppressed_when_out_of_stock():
    assert _confirm_automatic(page(in_stock=False), listing(), 8000) is None


def test_threshold_alert_fires_when_in_stock():
    deal = _confirm_automatic(page(in_stock=True), listing(), 8000)
    assert deal is not None and deal.kind == "under_threshold"


def test_steal_alert_suppressed_when_out_of_stock():
    assert _confirm_steal(page(price=4500, in_stock=False), stats(), "reason") is None


def test_steal_alert_fires_when_in_stock():
    deal = _confirm_steal(page(price=4500, in_stock=True), stats(), "reason")
    assert deal is not None and deal.kind == "steal"


def test_flag_restores_the_old_labelling_behaviour(monkeypatch):
    """Reversible: flipping the config back alerts with a label instead."""
    monkeypatch.setattr(config, "ALERT_ON_OUT_OF_STOCK", True)
    deal = _confirm_automatic(page(in_stock=False, note="selected variant out of stock"),
                              listing(), 8000)
    assert deal is not None and deal.in_stock is False


def test_dropped_listings_are_logged_loudly(caplog):
    """A silent drop is the failure mode this whole change risks."""
    import logging

    with caplog.at_level(logging.WARNING):
        _confirm_automatic(page(in_stock=False), listing(), 8000)
    assert any("DROPPED" in r.message for r in caplog.records)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
