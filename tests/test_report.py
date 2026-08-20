#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""The daily health report.

Its job is to distinguish "nothing to report" from "stopped working" — those
look identical from the alert stream alone.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon.report import Report, format_report, parse_log  # noqa: E402


def stamp(minutes_ago):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def since(hours=24):
    return datetime.now() - timedelta(hours=hours)


def test_counts_completed_checks():
    log = "\n".join(f"{stamp(m)} INFO    nothing qualifying right now" for m in (10, 20, 30))
    assert parse_log(log, since()).completed == 3


def test_ignores_entries_older_than_the_window():
    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    log = f"{old} INFO    nothing qualifying right now\n{stamp(5)} INFO    nothing qualifying right now"
    assert parse_log(log, since()).completed == 1


def test_collects_alerts():
    log = f"{stamp(5)} INFO    ALERT [steal] Timex ₹6159 something https://f.com/x"
    report = parse_log(log, since())
    assert len(report.alerts) == 1 and "Timex" in report.alerts[0]


def test_collects_failures_and_truncated_sweeps():
    log = "\n".join([
        f"{stamp(30)} ERROR   scrape failed: Page.goto: Timeout 45000ms exceeded.",
        f"{stamp(20)} INFO    no usable network (unreachable) — next attempt in 600s",
        f"{stamp(10)} WARNING alba/auto page 2: no listings (blocked or layout change)",
    ])
    report = parse_log(log, since())
    assert len(report.failures) == 2
    assert report.truncated and "alba/auto p2" in report.truncated[0]


def test_counts_skipped_ticks():
    log = "\n".join(f"{stamp(m)} INFO    skipping: throttled: ..." for m in (5, 15))
    assert parse_log(log, since()).skipped == 2


def test_malformed_lines_do_not_crash_it():
    assert parse_log("garbage\n\n   \nnot a timestamp at all", since()).completed == 0


def test_health_is_false_without_a_recent_success():
    stale = Report(since=since(), last_success=datetime.now() - timedelta(hours=9))
    assert stale.healthy is False
    assert format_report(stale)[0].startswith("⚠️")


def test_health_is_true_with_a_recent_success():
    fresh = Report(since=since(), completed=5, last_success=datetime.now() - timedelta(minutes=8))
    assert fresh.healthy is True
    assert format_report(fresh)[0].startswith("✅")


def test_report_says_so_when_nothing_ran():
    """The failure mode that matters: silence that looks like calm."""
    title, body = format_report(Report(since=since()))
    assert title.startswith("⚠️")
    assert "NO successful check" in body


def test_report_flags_that_steals_are_not_ready_yet():
    report = Report(since=since(), last_success=datetime.now(), products=1400, history_days=1)
    assert "steals need" in format_report(report)[1]
    ready = Report(since=since(), last_success=datetime.now(), history_days=9, steal_ready=True)
    assert "steals need" not in format_report(ready)[1]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_truncation_is_counted_under_both_wordings():
    """The message changed when page-retry landed; a parser that only knew the
    old wording would have quietly reported zero truncations forever."""
    old = f"{stamp(20)} WARNING alba/auto page 4: no listings (blocked or layout change)"
    new = f"{stamp(10)} WARNING alba/auto page 4: still empty after 1 retry(ies) — ending sweep"
    report = parse_log(old + "\n" + new, since())
    assert len(report.truncated) == 2


def test_recovered_retries_are_reported():
    log = f"{stamp(5)} INFO    alba/auto page 3 recovered on retry 1"
    report = parse_log(log, since())
    assert report.retries_recovered == 1
    assert "recovered by retry" in format_report(report)[1]
