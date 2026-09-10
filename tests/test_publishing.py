#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Affiliate links and the Telegram publisher.

Two things carry real-world consequences and are pinned hard: an unconfigured
setup must degrade to plain links rather than broken ones, and a post carrying
an affiliate link must carry a disclosure — undisclosed links are deceptive and
are what gets accounts banned.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, links, notify, sources  # noqa: E402
from watchmon.models import Deal, PriceStats  # noqa: E402

STORE = "https://store-a.example.com"
PRODUCT = f"{STORE}/some-watch/p/itm123"
AFFILIATE = {
    "publisher_id": "999",
    "redirector": "https://redirect.example.com",
    "retailers": {"a": "2276"},
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SITE_BASE", STORE)
    monkeypatch.setattr(config, "SITE_B_BASE", "")
    monkeypatch.setattr(config, "AFFILIATE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN_FILE", tmp_path / "no_token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_FILE", tmp_path / "no_chat")
    for var in ("AFFILIATE_CONFIG", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)


def configured(monkeypatch):
    monkeypatch.setenv("AFFILIATE_CONFIG", json.dumps(AFFILIATE))


def deal(kind="steal", median=11900, days=21, in_stock=True):
    return Deal(
        pid="itm123", url=PRODUCT, brand="acme", title="Acme Pro Diver Automatic",
        price=7200, kind=kind, reason="40% below its median", in_stock=in_stock,
        stats=PriceStats(days=days, median=median, min_ever=7500),
    )


# ----------------------------------------------------------------- links ----


def test_link_is_built_from_the_configured_ids(monkeypatch):
    configured(monkeypatch)
    url = links.affiliate_url(PRODUCT)
    assert url.startswith("https://redirect.example.com/visitretailer/2276?id=999&dl=")
    assert "store-a.example.com" in url  # destination is carried, url-encoded


def test_destination_is_encoded_not_appended_raw(monkeypatch):
    configured(monkeypatch)
    url = links.affiliate_url(PRODUCT)
    assert "dl=https%3A%2F%2F" in url


def test_unconfigured_degrades_to_the_plain_product_url():
    """An unconfigured checkout must still post a working link, not a dead one."""
    assert links.affiliate_url(PRODUCT) == PRODUCT
    assert links.is_configured() is False


def test_broken_config_degrades_rather_than_raising(monkeypatch):
    monkeypatch.setenv("AFFILIATE_CONFIG", "{not json")
    assert links.affiliate_url(PRODUCT) == PRODUCT


def test_a_storefront_with_no_retailer_id_is_not_monetised(monkeypatch):
    monkeypatch.setenv("AFFILIATE_CONFIG", json.dumps({**AFFILIATE, "retailers": {"b": "77"}}))
    assert links.affiliate_url(PRODUCT) == PRODUCT


def test_empty_url_is_handled():
    assert links.affiliate_url("") == ""


# ------------------------------------------------------------- the post ----


def test_post_leads_with_price_and_history(monkeypatch):
    configured(monkeypatch)
    text = notify.format_post(deal(), links.affiliate_url(PRODUCT))
    assert "₹7,200" in text
    assert "₹11,900" in text            # the median
    assert "save ₹4,700" in text        # the arithmetic done for the reader
    assert "21 days" in text            # the evidence nobody else has


def test_post_discloses_when_the_link_is_affiliate(monkeypatch):
    """Non-negotiable: an undisclosed affiliate link is deceptive."""
    configured(monkeypatch)
    text = notify.format_post(deal(), links.affiliate_url(PRODUCT))
    assert config.AFFILIATE_DISCLOSURE in text


def test_post_omits_disclosure_when_the_link_is_not_affiliate():
    text = notify.format_post(deal(), PRODUCT)
    assert config.AFFILIATE_DISCLOSURE not in text


def test_post_flags_out_of_stock():
    text = notify.format_post(deal(in_stock=False), PRODUCT)
    assert "out of stock" in text.lower()


def test_post_escapes_html_in_titles():
    d = deal()
    d.title = "Acme <b>Pro</b> & Co"
    text = notify.format_post(d, PRODUCT)
    assert "&lt;b&gt;" in text and "&amp;" in text


# ---------------------------------------------------------- the channel ----


def test_channel_is_unavailable_until_configured():
    assert notify.TelegramNotifier().available() is False


def test_channel_becomes_available_with_token_and_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@channel")
    assert notify.TelegramNotifier().available() is True


def test_only_publishable_kinds_are_accepted():
    channel = notify.TelegramNotifier()
    assert channel.accepts(deal(kind="steal")) is True
    assert channel.accepts(deal(kind="under_threshold")) is False


def test_a_steal_without_history_is_not_published():
    """No median means no evidence, and evidence is the point of the post."""
    assert notify.TelegramNotifier().accepts(deal(median=None)) is False


def test_publish_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@c")
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert notify.TelegramNotifier().publish(deal()) is False


# ------------------------------------------------------------- fan-out ----


class Spy:
    def __init__(self, name, accepts=None):
        self.name, self._accepts, self.sent, self.published = name, accepts, [], []

    def available(self):
        return True

    def send(self, title, message, url=None):
        self.sent.append(title)
        return True


class SpyPublisher(Spy):
    def accepts(self, deal):
        return deal.kind == "steal"

    def publish(self, deal):
        self.published.append(deal.pid)
        return True


def test_personal_channels_get_everything_publishers_get_only_steals():
    personal, publisher = Spy("ntfy"), SpyPublisher("telegram")
    notify.Notifier(channels=[personal, publisher]).announce(
        [deal(kind="steal"), deal(kind="under_threshold")], 8000
    )
    assert len(personal.sent) == 2
    assert publisher.published == ["itm123"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
