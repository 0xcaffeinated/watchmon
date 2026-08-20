#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Wake handling.

Origin: four dark-wake scrape timeouts compounded the backoff to 51 minutes,
so when the user opened the lid — good network, machine fully awake — the
monitor refused to run. Backoff was punishing the moment it should be keenest.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, runstate  # noqa: E402
from watchmon.runner import Monitor  # noqa: E402


# ------------------------------------------------------------ classify -----


def test_dark_wake_is_recent_wake_without_the_user():
    just_woke, present = runstate.classify_wake(since_wake=5, idle=9000)
    assert just_woke and not present


def test_user_wake_is_recent_wake_with_recent_input():
    just_woke, present = runstate.classify_wake(since_wake=5, idle=3)
    assert just_woke and present


def test_steady_state_is_neither():
    just_woke, present = runstate.classify_wake(since_wake=9000, idle=9000)
    assert not just_woke and not present


def test_user_present_long_after_a_wake_still_counts_as_present():
    _, present = runstate.classify_wake(since_wake=9000, idle=10)
    assert present


def test_unreadable_signals_are_treated_as_steady_state():
    """Missing signals must not make everything look like a dark wake, or real
    failures would stop escalating."""
    assert runstate.classify_wake(None, None) == (False, False)


def test_grace_boundary():
    assert runstate.classify_wake(config.WAKE_GRACE_SEC - 1, 9999)[0] is True
    assert runstate.classify_wake(config.WAKE_GRACE_SEC + 1, 9999)[0] is False


# ------------------------------------------------- transient vs real -------


def test_transient_failure_does_not_escalate():
    state = {"failures": 0}
    for _ in range(4):
        state = runstate.record_transient_failure(state, now=1000.0, reason="timeout")
    assert state["failures"] == 0
    assert state["backoff_until_ts"] == 1000.0 + config.WAKE_RETRY_SEC


def test_transient_failure_still_schedules_a_retry():
    state = runstate.record_transient_failure({}, now=1000.0, reason="timeout")
    assert state["backoff_until_ts"] > 1000.0
    assert "transient" in state["last_failure"]


def test_real_failures_still_escalate():
    state = {}
    for _ in range(3):
        state = runstate.record_failure(state, now=1000.0, reason="boom")
    assert state["failures"] == 3
    assert state["backoff_until_ts"] == 1000.0 + runstate.next_backoff(3)


def test_note_failure_routes_by_wake_state():
    dark = Monitor._note_failure({"failures": 0}, 1000.0, "timeout", dark_wake=True)
    real = Monitor._note_failure({"failures": 0}, 1000.0, "timeout", dark_wake=False)
    assert dark["failures"] == 0
    assert real["failures"] == 1


def test_four_dark_wakes_cannot_produce_the_51_minute_backoff():
    """The exact regression: 4 wake failures previously meant a 4,800s backoff."""
    state = {"failures": 0}
    now = 1000.0
    for _ in range(4):
        state = Monitor._note_failure(state, now, "Page.goto timeout", dark_wake=True)
    assert state["backoff_until_ts"] - now == config.WAKE_RETRY_SEC
    assert state["backoff_until_ts"] - now < 600


# ------------------------------------------------------- backoff clear -----


def test_clear_backoff_resets_both_fields():
    state = runstate.record_failure({}, now=1000.0, reason="x")
    cleared = runstate.clear_backoff(state)
    assert cleared["failures"] == 0 and cleared["backoff_until_ts"] == 0


def test_a_cleared_backoff_lets_the_run_proceed():
    state = {"failures": 4, "backoff_until_ts": 5000.0, "last_attempt_ts": 0}
    assert runstate.should_run(1000.0, state, 540)[0] is False
    assert runstate.should_run(1000.0, runstate.clear_backoff(state), 540)[0] is True


# --------------------------------------------------------- live signals ----


def test_seconds_since_wake_reads_the_real_system():
    value = runstate.seconds_since_wake()
    assert value is None or value >= 0


def test_seconds_since_user_input_reads_the_real_system():
    value = runstate.seconds_since_user_input()
    assert value is None or value >= 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
