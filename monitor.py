#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.40"]
# ///
"""Watch price monitor — CLI.

Alerts on Invicta / Casio / Timex / Alba watches that are either an automatic
under ₹8,000, or a steal against their own price history.

    ./monitor.py                 # one guarded check
    ./monitor.py --status        # throttle, backoff, history summary
    ./monitor.py --history <pid> # price series for one product
    ./monitor.py --dry-run       # scrape + print JSON, no alerts, no state
    ./monitor.py --force         # ignore throttle and backoff
    ./monitor.py --test-notify   # prove both channels reach you

Scheduling lives in install.sh (launchd, every 10 minutes by default).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchmon import config  # noqa: E402
from watchmon.history import PriceHistory  # noqa: E402
from watchmon.notify import Notifier, find_notifier, ntfy_topic  # noqa: E402
from watchmon.runner import Monitor  # noqa: E402
from watchmon.runstate import (  # noqa: E402
    effective_min_interval,
    lid_closed,
    load_state,
    should_run,
)


def setup_logging(verbose: bool) -> None:
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler(sys.stdout)],
    )


def print_status(threshold: int) -> int:
    state = load_state()
    now = time.time()
    shut = lid_closed()
    min_interval = effective_min_interval(config.MIN_INTERVAL_SEC, shut)
    go, why = should_run(now, state, min_interval)

    def ago(ts):
        return f"{int(now - ts)}s ago" if ts else "never"

    print(f"brands:         {', '.join(b.title() for b in config.BRANDS)}")
    print(f"lid:            {'closed' if shut else 'open'} (interval {min_interval}s)")
    print(f"last attempt:   {ago(state.get('last_attempt_ts'))}")
    print(f"last success:   {ago(state.get('last_success_ts'))}")
    print(f"failures:       {state.get('failures', 0)}   last: {state.get('last_failure') or '-'}")
    print(f"next run:       {'now' if go else why}")

    summary = PriceHistory(config.HISTORY_DB).summary()
    print(
        f"history:        {summary['products']} product(s), {summary['price_points']} price point(s)"
    )
    if summary["first_day"]:
        print(f"                {summary['first_day']} .. {summary['last_day']}")
    for brand, count in summary["by_brand"]:
        print(f"                {brand or '?':10} {count}")
    days_needed = config.STEAL_MIN_HISTORY_DAYS
    print(f"steal rule:     >={config.STEAL_DISCOUNT:.0%} below 30d median, at/near all-time low,")
    print(f"                after >={days_needed} days of history, price >= ₹{config.STEAL_MIN_PRICE:,}")

    alerted = state.get("alerted", {})
    print(f"alerted:        {len(alerted)} active")
    for key, price in alerted.items():
        print(f"                ₹{price:,}  {key}")
    return 0


def print_history(pid: str) -> int:
    history = PriceHistory(config.HISTORY_DB)
    product = history.product(pid)
    series = history.series(pid)
    if not series:
        print(f"no history for {pid}")
        return 1
    if product:
        print(f"{product['title']}\n{product['url']}\n")
    prices = [p for _, p in series]
    for day, price in series:
        bar = "█" * max(1, round(20 * price / max(prices)))
        print(f"  {day}  ₹{price:>7,}  {bar}")
    print(f"\n  low ₹{min(prices):,}   high ₹{max(prices):,}   days {len(series)}")
    return 0


def test_notify() -> int:
    notifier = Notifier()
    results = notifier.send(
        "🔥 STEAL — Timex ₹4,499",
        "Sample alert — 31% below its ₹6,499 median. Tap to open the listing.",
        config.SITE_BASE + "/search?q=timex+watch",
    )
    topic = ntfy_topic()
    print(f"macOS banner: {'sent' if results.get('macos') else 'FAILED'} "
          f"(terminal-notifier: {find_notifier() or 'not installed'})")
    if not topic:
        print("mobile push:  NOT CONFIGURED — write a topic to ntfy_topic.txt")
    else:
        print(f"mobile push:  {'delivered to ' + topic if results.get('ntfy') else 'FAILED — see log'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--threshold",
        type=int,
        default=int(os.environ.get("WATCH_THRESHOLD", config.THRESHOLD_INR)),
        help="the automatic-watch price ceiling (default ₹8,000)",
    )
    ap.add_argument("--dry-run", action="store_true", help="scrape and print; no alerts, no state")
    ap.add_argument("--force", action="store_true", help="ignore the throttle and any backoff")
    ap.add_argument("--status", action="store_true", help="print run state and history summary")
    ap.add_argument("--history", metavar="PID", help="price series for one product id")
    ap.add_argument("--test-notify", action="store_true", help="fire a sample alert on all channels")
    ap.add_argument("--report", action="store_true", help="print a 24h health report")
    ap.add_argument("--report-push", action="store_true", help="send that report to phone + Mac")
    ap.add_argument("--show-browser", action="store_true", help="run Chromium headed (debugging)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    if args.status:
        return print_status(args.threshold)
    if args.history:
        return print_history(args.history)
    if args.test_notify:
        return test_notify()
    if args.report or args.report_push:
        from watchmon.report import format_report, gather

        title, body = format_report(gather())
        print(title)
        print(body)
        if args.report_push:
            Notifier().send(title, body)
        return 0

    monitor = Monitor(threshold=args.threshold, headless=not args.show_browser)
    return monitor.run(force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
