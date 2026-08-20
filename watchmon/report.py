"""Daily health report on the monitor itself.

Answers "did it actually work overnight?" — which the alert stream cannot,
because a monitor that silently stopped looks exactly like a monitor with
nothing to report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from .history import PriceHistory
from .runstate import load_state

LOG_TS = "%Y-%m-%d %H:%M:%S"


@dataclass
class Report:
    since: datetime
    completed: int = 0
    alerts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    retries_recovered: int = 0
    dropped_oos: list[str] = field(default_factory=list)
    skipped: int = 0
    last_success: datetime | None = None
    products: int = 0
    price_points: int = 0
    history_days: int = 0
    steal_ready: bool = False

    @property
    def healthy(self) -> bool:
        """Ran recently and isn't stuck in backoff."""
        if self.last_success is None:
            return False
        return (datetime.now() - self.last_success) < timedelta(hours=6)


def parse_log(text: str, since: datetime) -> Report:
    """Read the run log for the window. Pure — takes text, not a path."""
    report = Report(since=since)

    for line in text.splitlines():
        stamp = line[:19]
        try:
            when = datetime.strptime(stamp, LOG_TS)
        except ValueError:
            continue
        if when < since:
            continue

        if "nothing qualifying" in line or "under_threshold:" in line or "steal:" in line:
            if "nothing qualifying" in line:
                report.completed += 1
                report.last_success = when
        if "nothing new to announce" in line:
            report.last_success = when
        if "ALERT" in line:
            report.completed += 0
            report.alerts.append(line.split("ALERT", 1)[1].strip()[:110])
            report.last_success = when
        if "scrape failed" in line or "no usable network" in line:
            report.failures.append(f"{when.strftime('%H:%M')} {line.split('INFO')[-1].split('ERROR')[-1].strip()[:90]}")
        # Matches both wordings: the pre-retry "no listings (blocked...)" and
        # the current "still empty after N retry(ies)". Kept broad on purpose —
        # a parser that silently stops matching turns a real problem into a
        # clean-looking report.
        if "no listings (blocked" in line or "still empty after" in line:
            match = re.search(r"(\S+) page (\d+):", line)
            if match:
                report.truncated.append(f"{when.strftime('%H:%M')} {match.group(1)} p{match.group(2)}")
        if "recovered on retry" in line:
            report.retries_recovered += 1
        if "DROPPED" in line:
            report.dropped_oos.append(line.split("DROPPED", 1)[1].strip()[:90])
        if "skipping:" in line:
            report.skipped += 1

    return report


def gather(hours: int = 24) -> Report:
    """Build the report from the log, state file and history database."""
    since = datetime.now() - timedelta(hours=hours)
    try:
        text = config.LOG_FILE.read_text(errors="replace")
    except OSError:
        text = ""
    report = parse_log(text, since)

    state = load_state()
    if state.get("last_success_ts"):
        report.last_success = datetime.fromtimestamp(state["last_success_ts"])

    try:
        summary = PriceHistory(config.HISTORY_DB).summary()
        report.products = summary["products"]
        report.price_points = summary["price_points"]
        if summary["first_day"] and summary["last_day"]:
            first = datetime.strptime(summary["first_day"], "%Y-%m-%d")
            last = datetime.strptime(summary["last_day"], "%Y-%m-%d")
            report.history_days = (last - first).days + 1
        report.steal_ready = report.history_days >= config.STEAL_MIN_HISTORY_DAYS
    except Exception:  # noqa: BLE001 - a broken DB must still produce a report
        pass

    return report


def format_report(report: Report) -> tuple[str, str]:
    """(title, body) for notification and stdout."""
    mark = "✅" if report.healthy else "⚠️"
    title = f"{mark} Watch monitor — {report.completed} checks/24h"

    lines = []
    if report.last_success:
        age = int((datetime.now() - report.last_success).total_seconds() / 60)
        lines.append(f"last success {age}m ago")
    else:
        lines.append("NO successful check in the window")

    if report.alerts:
        lines.append(f"{len(report.alerts)} alert(s):")
        lines.extend(f"  {a}" for a in report.alerts[:5])
    else:
        lines.append("no alerts")

    if report.failures:
        lines.append(f"{len(report.failures)} failure(s): {report.failures[-1]}")
    if report.truncated:
        lines.append(f"{len(report.truncated)} truncated sweep(s), latest {report.truncated[-1]}")
    if report.retries_recovered:
        lines.append(f"{report.retries_recovered} page(s) recovered by retry")
    if report.dropped_oos:
        lines.append(f"{len(report.dropped_oos)} qualifying but OUT OF STOCK (not alerted):")
        lines.extend(f"  {d}" for d in report.dropped_oos[:3])
    if report.skipped:
        lines.append(f"{report.skipped} tick(s) skipped (throttle/backoff/sleep)")

    lines.append(
        f"history {report.products} products, {report.history_days}d span"
        + ("" if report.steal_ready else f" (steals need {config.STEAL_MIN_HISTORY_DAYS}d)")
    )
    return title, "\n".join(lines)
