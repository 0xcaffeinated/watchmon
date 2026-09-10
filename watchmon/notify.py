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
        "tags": ["moneybag"],
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
        # Named after the rule that matched, so a second category does not
        # arrive announcing itself as a watch.
        what = deal.rule or "match"
        title = f"🎯 {brand} ₹{deal.price:,}{flag}"
        message = f"{deal.title[:70]} — {what}, below ₹{threshold:,}. Tap to open."
    return title, message


class Notifier:
    """Fans one alert out to every available channel."""

    def __init__(self, channels=None):
        if channels is None:
            channels = [NtfyNotifier(), MacNotifier(), TelegramNotifier()]
        self.channels = channels

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
            for channel in self.channels:
                if not channel.available():
                    continue
                # A channel may decline a deal — Telegram is an audience, and
                # a private price nudge is not audience content.
                accepts = getattr(channel, "accepts", None)
                if accepts is not None and not accepts(deal):
                    continue
                try:
                    if hasattr(channel, "publish"):
                        published = channel.publish(deal)
                        log.info("published to %s: %s", channel.name, published)
                    else:
                        channel.send(title, message, deal.url)
                except Exception as exc:  # noqa: BLE001 - never kill the run
                    log.warning("%s notifier raised %s", channel.name, exc)
            log.info(
                "ALERT [%s] %s ₹%s %s — %s | %s",
                deal.kind,
                (deal.brand or "?").title(),
                deal.price,
                deal.title[:55],
                deal.reason or deal.stock_note,
                deal.url,
            )


# ------------------------------------------------------------ publishing ----


def _file_or_env(env_name: str, path) -> str | None:
    """Env first (CI secret), then a gitignored file (local)."""
    from_env = os.environ.get(env_name, "").strip()
    if from_env:
        return from_env
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def telegram_token() -> str | None:
    return _file_or_env("TELEGRAM_TOKEN", config.TELEGRAM_TOKEN_FILE)


def telegram_chat() -> str | None:
    return _file_or_env("TELEGRAM_CHAT_ID", config.TELEGRAM_CHAT_FILE)


def _escape(text: str) -> str:
    """Telegram HTML mode: only these three need escaping."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_post(deal: Deal, link: str) -> str:
    """A post that earns its place: the price, and why it is notable.

    The price history is the whole differentiator — anyone can write "80% off",
    almost nobody can say what this product has actually cost for a month.
    """
    from . import links as _links

    title = _escape(deal.title[:90])
    lines = [f"🔥 <b>{title}</b>", "", f"<b>₹{deal.price:,}</b>"]

    stats = deal.stats
    if stats.median:
        saving = stats.median - deal.price
        lines.append(f"Usually ₹{stats.median:,} — you save ₹{saving:,}")
    if stats.min_ever is not None and stats.days:
        lines.append(
            f"📉 Cheapest in {stats.days} days of tracking "
            f"(previous low ₹{stats.min_ever:,})"
        )
    if not deal.in_stock:
        lines.append("⚠️ Listing shows out of stock — check before ordering")

    lines += ["", f'<a href="{_escape(link)}">Buy now →</a>']
    if _links.is_configured():
        lines += ["", f"<i>{_escape(config.AFFILIATE_DISCLOSURE)}</i>"]
    return "\n".join(lines)


class TelegramNotifier:
    """Publishes deals to a channel. Audience-facing, so it filters."""

    name = "telegram"

    def available(self) -> bool:
        return bool(telegram_token() and telegram_chat())

    def accepts(self, deal: Deal) -> bool:
        """Only kinds worth an audience, and only with evidence behind them."""
        if deal.kind not in config.TELEGRAM_PUBLISH_KINDS:
            return False
        return bool(deal.stats.median)

    def send(self, title: str, message: str, url: str | None = None) -> bool:
        return self._post(message if message else title)

    def publish(self, deal: Deal) -> bool:
        from . import links as _links

        link = _links.affiliate_url(deal.url)
        return self._post(format_post(deal, link))

    def _post(self, text: str) -> bool:
        token, chat = telegram_token(), telegram_chat()
        if not (token and chat):
            return False

        import urllib.error
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode(
            {
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            }
        ).encode()
        request = urllib.request.Request(
            f"{config.TELEGRAM_API}/bot{token}/sendMessage", data=body, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=config.TELEGRAM_TIMEOUT_SEC) as r:
                ok = 200 <= r.status < 300
                if not ok:
                    log.warning("telegram returned HTTP %s", r.status)
                return ok
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Publishing must never take down a check or trip the backoff.
            log.warning("telegram publish failed (%s)", exc)
            return False
