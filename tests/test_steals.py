#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""The steal rule, and the alerting that sits on top of it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, steals  # noqa: E402
from watchmon.models import Deal, PriceStats  # noqa: E402
from watchmon.runner import decide_alerts  # noqa: E402


def stats(days=10, median=10000, min_ever=9000):
    return PriceStats(days=days, median=median, min_ever=min_ever)


# ------------------------------------------------------------- the rule -----


def test_steal_needs_both_a_discount_and_an_all_time_low():
    # 55% off the median and below the old low.
    ok, reason = steals.is_steal(4500, stats(median=10000, min_ever=4600))
    assert ok
    assert "55%" in reason and "10,000" in reason


def test_small_discount_is_not_a_steal():
    ok, reason = steals.is_steal(8500, stats(median=10000, min_ever=8000))
    assert not ok
    assert f"need {config.STEAL_DISCOUNT:.0%}" in reason


def test_big_discount_but_not_near_the_low_is_not_a_steal():
    """Cheap against the median yet well above what it has actually sold for:
    that is a median skewed by a price spike, not a bargain."""
    ok, reason = steals.is_steal(4500, stats(median=10000, min_ever=3000))
    assert not ok and "all-time low" in reason


def test_within_tolerance_of_the_low_still_counts():
    # 2% tolerance: matching the previous low to the rupee is too strict.
    assert steals.is_steal(4590, stats(median=10000, min_ever=4500))[0] is True
    assert steals.is_steal(4700, stats(median=10000, min_ever=4500))[0] is False


def test_no_alert_before_enough_history():
    """The single most important guard: a product seen for the first time has
    no baseline, so every first sighting would otherwise look like a steal."""
    ok, reason = steals.is_steal(5000, stats(days=2, median=10000, min_ever=10000))
    assert not ok and "need 5" in reason


def test_history_guard_holds_at_the_boundary():
    just_short = stats(days=config.STEAL_MIN_HISTORY_DAYS - 1, median=10000, min_ever=4600)
    just_enough = stats(days=config.STEAL_MIN_HISTORY_DAYS, median=10000, min_ever=4600)
    assert steals.is_steal(4500, just_short)[0] is False
    assert steals.is_steal(4500, just_enough)[0] is True


def test_cheap_items_are_ignored():
    # A ₹300 strap dropping 40% is not news.
    ok, reason = steals.is_steal(300, stats(median=1000, min_ever=300))
    assert not ok and "floor" in reason


def test_missing_price_or_history_is_never_a_steal():
    assert steals.is_steal(None, stats())[0] is False
    assert steals.is_steal(5000, PriceStats())[0] is False


def test_reason_is_returned_for_near_misses_too():
    """So the log can answer 'why didn't it alert?'."""
    _, reason = steals.is_steal(9000, stats(median=10000, min_ever=8000))
    assert "9,000" in reason and "10,000" in reason


def test_find_steals_screens_many():
    priced = {"a": 4500, "b": 9900, "c": 500}
    st = {
        "a": stats(median=10000, min_ever=4600),
        "b": stats(median=10000, min_ever=9000),
        "c": stats(median=1000, min_ever=500),
    }
    assert [pid for pid, _ in steals.find_steals(priced, st)] == ["a"]


def test_thresholds_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "STEAL_DISCOUNT", 0.9)
    assert steals.is_steal(7000, stats(median=10000, min_ever=7100))[0] is False
    monkeypatch.setattr(config, "STEAL_DISCOUNT", 0.1)
    assert steals.is_steal(7000, stats(median=10000, min_ever=7100))[0] is True


# ------------------------------------------------------------- alerting -----


def D(pid="a", price=7000, kind="steal"):
    return Deal(pid=pid, url="u", brand="timex", title="Timex", price=price, kind=kind)


def test_first_sighting_alerts():
    to_alert, state = decide_alerts([D()], {"alerted": {}})
    assert [d.pid for d in to_alert] == ["a"]
    assert state["alerted"] == {"steal:a": 7000}


def test_same_price_does_not_re_alert():
    assert decide_alerts([D()], {"alerted": {"steal:a": 7000}})[0] == []


def test_further_drop_alerts_again():
    to_alert, _ = decide_alerts([D(price=6500)], {"alerted": {"steal:a": 7000}})
    assert [d.pid for d in to_alert] == ["a"]


def test_the_two_rules_alert_independently():
    """One watch can be both under ₹8,000 and a steal; muting one must not
    mute the other."""
    deals = [D(kind="steal"), D(kind="under_threshold")]
    to_alert, state = decide_alerts(deals, {"alerted": {"steal:a": 7000}})
    assert [d.kind for d in to_alert] == ["under_threshold"]
    assert set(state["alerted"]) == {"steal:a", "under_threshold:a"}


def test_dropping_out_rearms():
    _, state = decide_alerts([], {"alerted": {"steal:a": 7000}})
    assert state["alerted"] == {}
    to_alert, _ = decide_alerts([D()], state)
    assert [d.pid for d in to_alert] == ["a"]


def test_decide_alerts_preserves_run_state():
    state = {"alerted": {}, "failures": 3, "backoff_until_ts": 999.0}
    _, new = decide_alerts([D()], state)
    assert new["failures"] == 3 and new["backoff_until_ts"] == 999.0


def test_deal_serialises_history_context():
    deal = Deal(
        pid="a", url="u", brand="timex", title="T", price=7000, kind="steal",
        reason="30% below", stats=PriceStats(days=9, median=10000, min_ever=7100),
    )
    row = deal.to_dict()
    assert row["kind"] == "steal" and row["median"] == 10000 and row["history_days"] == 9


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
