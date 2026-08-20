#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Wiring test: history -> stats -> steal rule -> Deal, without a browser.

The steal feature cannot demonstrate itself against the live site until five
days of history exist, so this seeds history by hand and drives the real
Monitor methods over it.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config  # noqa: E402
from watchmon.history import PriceHistory  # noqa: E402
from watchmon.models import Listing  # noqa: E402
from watchmon.notify import Notifier  # noqa: E402
from watchmon.runner import Monitor  # noqa: E402
from watchmon.scraper import ProductPage  # noqa: E402


def ts_for(offset):
    base = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=offset)).timestamp()


@pytest.fixture
def monitor(tmp_path):
    """A Monitor with real history and a notifier that sends nowhere."""
    return Monitor(
        threshold=8000,
        history=PriceHistory(tmp_path / "h.db"),
        notifier=Notifier(channels=[]),
    )


def seed(history, pid, price, days):
    for offset in range(-days, 0):
        history.record(
            [Listing(pid=pid, url=f"https://f.com/x/p/{pid}", title="Timex Watch",
                     price=price, brand="timex")],
            ts_for(offset),
        )


def page(pid="p1", price=4500, title="TIMEX Automatic Watch"):
    return ProductPage(
        pid=pid, url=f"https://f.com/x/p/{pid}", title=title, heading=title,
        price=price, movement="Mechanical Automatic", in_stock=True, stock_note="in stock",
    )


def test_a_steal_is_found_and_confirmed(monitor):
    seed(monitor.history, "p1", 10000, days=8)
    tracked = [Listing(pid="p1", url="https://f.com/x/p/p1", title="Timex Watch",
                       price=4500, brand="timex")]

    candidates = monitor._screen_steals(tracked, ts_for(0))
    assert len(candidates) == 1

    _listing, stats, reason = candidates[0]
    assert stats.median == 10000

    from watchmon.runner import _confirm_steal

    deal = _confirm_steal(page(price=4500), stats, reason)
    assert deal is not None
    assert deal.kind == "steal"
    assert deal.price == 4500
    assert "55%" in deal.reason
    assert deal.alert_key == "steal:p1"


def test_a_normal_price_is_not_a_steal(monitor):
    seed(monitor.history, "p1", 10000, days=8)
    tracked = [Listing(pid="p1", url="u", title="Timex Watch", price=9800, brand="timex")]
    assert monitor._screen_steals(tracked, ts_for(0)) == []


def test_a_brand_new_product_never_steals(monitor):
    """First sighting has no baseline; without this guard every newly listed
    watch would look like an all-time low."""
    tracked = [Listing(pid="new", url="u", title="Timex Watch", price=999, brand="timex")]
    assert monitor._screen_steals(tracked, ts_for(0)) == []


def test_todays_price_cannot_become_its_own_baseline(monitor):
    """Record today first, then screen: the drop must still register, because
    stats exclude today."""
    seed(monitor.history, "p1", 10000, days=8)
    now = ts_for(0)
    monitor.history.record(
        [Listing(pid="p1", url="u", title="Timex Watch", price=4500, brand="timex")], now
    )
    tracked = [Listing(pid="p1", url="u", title="Timex Watch", price=4500, brand="timex")]
    assert len(monitor._screen_steals(tracked, now)) == 1


def test_confirmation_uses_the_product_page_price(monitor):
    """If the card said ₹7,000 but the page says ₹9,900, no alert."""
    from watchmon.runner import _confirm_steal

    seed(monitor.history, "p1", 10000, days=8)
    _, stats, reason = monitor._screen_steals(
        [Listing(pid="p1", url="u", title="Timex Watch", price=4500, brand="timex")], ts_for(0)
    )[0]
    assert _confirm_steal(page(price=9900), stats, reason) is None


def test_muted_series_never_steals(monitor, monkeypatch):
    from watchmon.runner import _confirm_steal

    seed(monitor.history, "p1", 10000, days=8)
    _, stats, reason = monitor._screen_steals(
        [Listing(pid="p1", url="u", title="Timex Watch", price=4500, brand="timex")], ts_for(0)
    )[0]
    muted = page(price=7000, title="TIMEX Automatic TWEG208SMU07")
    assert _confirm_steal(muted, stats, reason) is None


def test_history_records_every_watched_brand(monitor):
    tracked = [
        Listing(pid="a", url="u", title="Casio Quartz", price=3000, brand="casio"),
        Listing(pid="b", url="u", title="Alba Analog", price=5850, brand="alba"),
        Listing(pid="c", url="u", title="Invicta Pro Diver", price=10499, brand="invicta"),
    ]
    assert monitor.history.record(tracked, ts_for(0)) == 3
    assert monitor.history.summary()["products"] == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
