"""Alert delivery.

Each channel is a class with the same `send()` shape, and `Notifier` fans out
to whichever are configured. A channel that fails logs and returns False — a
dead phone must never take down the check or trip the failure backoff.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from . import config
from .models import Deal

log = logging.getLogger("watchmon.notify")


def find_notifier(which=shutil.which, exists=os.path.exists) -> str | None:
    """Locate terminal-notifier, which makes macOS alerts clickable.

    PATH alone is not enough: launchd hands the job a bare
    /usr/bin:/bin:/usr/sbin:/sbin, so a Homebrew install is invisible to
    which() in the scheduled run though it resolves fine in a shell.
    """
    found = which("terminal-notifier")
    if found:
        return found
    for path in config.NOTIFIER_PATHS:
        if exists(path):
            return path
    return None


def ntfy_topic() -> str | None:
    """Topic for phone push, or None if unconfigured.

    File first, env as an override: launchd passes a bare environment, so
    anything exported in a shell profile is invisible to the scheduled run.
    """
    from_env = os.environ.get("NTFY_TOPIC", "").strip()
    if from_env:
        return from_env
    try:
        return config.NTFY_TOPIC_FILE.read_text().strip() or None
    except OSError:
        return None


def build_ntfy_payload(topic: str, title: str, message: str, url: str | None = None) -> dict:
    """JSON body for ntfy.

    A body, never HTTP headers: ntfy's header API is latin-1 and every one of
    these titles carries a ₹.
    """
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": 4,
        "tags": ["watch"],
    }
    if url:
        payload["click"] = url
    return payload


class MacNotifier:
    """Native banner + sound on this Mac."""

    name = "macos"

    def available(self) -> bool:
        return True

    def send(self, title: str, message: str, url: str | None = None) -> bool:
        binary = find_notifier()
        if binary:
            cmd = [binary, "-title", title, "-message", message, "-sound", "Glass"]
            if url:
                cmd += ["-open", url]
            subprocess.run(cmd, check=False)
            return True

        script = 'display notification "{}" with title "{}" sound name "Glass"'.format(
            message.replace('"', "'"), title.replace('"', "'")
        )
        subprocess.run(["osascript", "-e", script], check=False)
        return True


class NtfyNotifier:
    """Push to the phone via ntfy.sh."""

    name = "ntfy"

    def available(self) -> bool:
        return ntfy_topic() is not None

    def send(self, title: str, message: str, url: str | None = None) -> bool:
        topic = ntfy_topic()
        if not topic:
            return False

        import urllib.error
        import urllib.request

        body = json.dumps(build_ntfy_payload(topic, title, message, url)).encode("utf-8")
        request = urllib.request.Request(
            config.NTFY_SERVER,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=config.NTFY_TIMEOUT_SEC) as response:
                if not 200 <= response.status < 300:
                    log.warning("ntfy returned HTTP %s", response.status)
                    return False
                return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("mobile push failed (%s) — other channels still sent", exc)
            return False


def format_deal(deal: Deal, threshold: int) -> tuple[str, str]:
    """Title and body for one deal. Steals lead with the discount."""
    brand = (deal.brand or "?").title()
    flag = "" if deal.in_stock else " ⚠️ shows out of stock"

    if deal.kind == "steal":
        title = f"🔥 STEAL — {brand} ₹{deal.price:,}{flag}"
        message = f"{deal.title[:70]} — {deal.reason}"
    else:
        title = f"⌚ {brand} automatic ₹{deal.price:,}{flag}"
        message = f"{deal.title[:70]} — below ₹{threshold:,}. Tap to open the listing."
    return title, message


class Notifier:
    """Fans one alert out to every available channel."""

    def __init__(self, channels=None):
        self.channels = channels if channels is not None else [NtfyNotifier(), MacNotifier()]

    def send(self, title: str, message: str, url: str | None = None) -> dict[str, bool]:
        results = {}
        for channel in self.channels:
            if not channel.available():
                results[channel.name] = False
                continue
            try:
                results[channel.name] = channel.send(title, message, url)
            except Exception as exc:  # noqa: BLE001 - a channel must not kill the run
                log.warning("%s notifier raised %s", channel.name, exc)
                results[channel.name] = False
        return results

    def announce(self, deals: list[Deal], threshold: int) -> None:
        for deal in deals:
            title, message = format_deal(deal, threshold)
            self.send(title, message, deal.url)
            log.info(
                "ALERT [%s] %s ₹%s %s — %s | %s",
                deal.kind,
                (deal.brand or "?").title(),
                deal.price,
                deal.title[:55],
                deal.reason or deal.stock_note,
                deal.url,
            )
