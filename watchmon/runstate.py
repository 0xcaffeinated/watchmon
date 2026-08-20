"""Whether a run should happen at all, and the bookkeeping that decides it.

Throttle, failure backoff, network health, lid state and the single-instance
lock. The decision functions take `now` as an argument so tests drive time by
hand instead of sleeping.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import socket
import subprocess
import time

from . import config
from .models import now_iso

log = logging.getLogger("watchmon.runstate")


# ------------------------------------------------------------- persistence --


def load_state() -> dict:
    if not config.STATE_FILE.exists():
        return {"alerted": {}}
    try:
        return json.loads(config.STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("state file unreadable — starting fresh")
        return {"alerted": {}}


def save_state(state: dict) -> None:
    config.STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- backoff ---


def next_backoff(failures: int, base: int | None = None, cap: int | None = None) -> int:
    """Exponential backoff, capped. 0 failures means no backoff."""
    base = config.BACKOFF_BASE_SEC if base is None else base
    cap = config.BACKOFF_MAX_SEC if cap is None else cap
    if failures <= 0:
        return 0
    return min(cap, base * (2 ** (failures - 1)))


def record_attempt(state: dict, now: float) -> dict:
    """Stamp the attempt *before* the work.

    Up front on purpose: if the scrape hangs or the process is killed, the next
    tick still sees a recent attempt and throttles instead of piling on.
    """
    return {**state, "last_attempt_ts": now, "updated": now_iso()}


def record_failure(state: dict, now: float, reason: str) -> dict:
    failures = int(state.get("failures", 0)) + 1
    return {
        **state,
        "failures": failures,
        "last_failure": reason,
        "backoff_until_ts": now + next_backoff(failures),
        "updated": now_iso(),
    }


def record_success(state: dict, now: float) -> dict:
    return {
        **state,
        "failures": 0,
        "last_failure": None,
        "backoff_until_ts": 0,
        "last_success_ts": now,
        "updated": now_iso(),
    }


# --------------------------------------------------------------- throttle ---


def effective_min_interval(base: int, lid_closed: bool) -> int:
    return base * config.LID_CLOSED_MULTIPLIER if lid_closed else base


def should_run(now: float, state: dict, min_interval: int, force: bool = False) -> tuple[bool, str]:
    """Two independent guards: catch-up throttle, and failure backoff."""
    if force:
        return True, "forced"

    backoff_until = state.get("backoff_until_ts") or 0
    if now < backoff_until:
        return False, (
            f"backing off for another {int(backoff_until - now)}s "
            f"after {state.get('failures', 0)} failure(s)"
        )

    since = now - (state.get("last_attempt_ts") or 0)
    if since < min_interval:
        return False, f"throttled: last attempt {int(since)}s ago, minimum is {min_interval}s"

    return True, "due"


# ---------------------------------------------------------------- signals ---


def seconds_since_wake(now: float | None = None) -> float | None:
    """How long since the Mac last woke, or None if unreadable.

    `kern.waketime` is cheap and exact — parsing `pmset -g log` for the same
    answer means reading megabytes of text on every tick.
    """
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.waketime"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"sec\s*=\s*(\d+)", out)
    if not match:
        return None
    return (time.time() if now is None else now) - int(match.group(1))


def seconds_since_user_input() -> float | None:
    """Idle time from the HID system, or None if unreadable.

    Stands in for "is the user actually here?" — the display power state is
    unavailable on this machine, so input recency is the usable signal.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not match:
        return None
    return int(match.group(1)) / 1e9


def classify_wake(
    since_wake: float | None,
    idle: float | None,
    grace: int | None = None,
    present: int | None = None,
) -> tuple[bool, bool]:
    """Return (just_woke, user_present). Pure, so the tests can drive it."""
    grace = config.WAKE_GRACE_SEC if grace is None else grace
    present = config.USER_PRESENT_SEC if present is None else present
    just_woke = since_wake is not None and since_wake < grace
    user_present = idle is not None and idle < present
    return just_woke, user_present


def record_transient_failure(state: dict, now: float, reason: str) -> dict:
    """A failure we refuse to hold against the monitor.

    Schedules a near-term retry without touching the consecutive-failure count,
    so a run of dark-wake timeouts can't escalate into an hours-long backoff.
    """
    return {
        **state,
        "last_failure": f"{reason} (transient, at wake)",
        "backoff_until_ts": now + config.WAKE_RETRY_SEC,
        "updated": now_iso(),
    }


def clear_backoff(state: dict) -> dict:
    return {**state, "failures": 0, "backoff_until_ts": 0, "updated": now_iso()}


def lid_closed() -> bool:
    """True if the clamshell is shut. False if unknown."""
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return '"AppleClamshellState" = Yes' in out.stdout


def probe_network() -> tuple[bool, float | None]:
    """One TCP handshake to the site. Returns (reachable, seconds)."""
    started = time.monotonic()
    try:
        with socket.create_connection(
            (config.NET_PROBE_HOST, config.NET_PROBE_PORT),
            timeout=config.NET_CONNECT_TIMEOUT,
        ):
            return True, time.monotonic() - started
    except OSError:
        return False, None


def classify_probe(ok: bool, latency: float | None, slow_after: float | None = None) -> tuple[bool, str]:
    """Turn a raw probe into a usable/not-usable verdict."""
    slow_after = config.NET_SLOW_SEC if slow_after is None else slow_after
    if not ok:
        return False, "unreachable"
    if latency is not None and latency > slow_after:
        return False, f"too slow ({latency:.1f}s handshake)"
    return True, f"ok ({latency:.2f}s)" if latency is not None else "ok"


def wait_for_network(retries: int | None = None, delay: int | None = None) -> tuple[bool, str]:
    """Probe until the network is usable, or give up.

    Exists for wake-from-sleep: launchd fires the catch-up run the instant the
    machine wakes, well before Wi-Fi has associated.
    """
    retries = config.NET_RETRIES if retries is None else retries
    delay = config.NET_RETRY_DELAY_SEC if delay is None else delay

    reason = "not probed"
    for attempt in range(1, retries + 1):
        usable, reason = classify_probe(*probe_network())
        if usable:
            if attempt > 1:
                log.info("network came up after %d probe(s): %s", attempt, reason)
            return True, reason
        if attempt < retries:
            log.info("network %s — retrying in %ds (%d/%d)", reason, delay, attempt, retries)
            time.sleep(delay)
    return False, reason


@contextlib.contextmanager
def single_instance():
    """Yield True if we got the lock, False if another run holds it."""
    handle = open(config.LOCK_FILE, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            handle.write(str(os.getpid()))
            handle.flush()
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()
