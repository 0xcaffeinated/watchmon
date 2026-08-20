#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Ported from the pre-package suite: pure parsing, filtering and run-state.

Kept intact so the refactor is provably behaviour-preserving — every rule that
was pinned before is still pinned, just against the new module layout.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, notify, parsing, runstate  # noqa: E402
from watchmon.models import Listing  # noqa: E402


def L(pid, title, price, url=""):
    return Listing(pid=pid, url=url or f"https://shop.example.com/x/p/{pid}", title=title, price=price)


# ------------------------------------------------------------ parse_price ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("INVICTA Pro Diver ₹11,089₹16,99534% off", 11089),
        ("₹7,999", 7999),
        ("₹ 8,000 off", 8000),
        ("₹999", 999),
        ("no price here", None),
        ("", None),
    ],
)
def test_parse_price(text, expected):
    assert parsing.parse_price(text) == expected


def test_parse_price_takes_the_selling_price_first():
    # The site renders selling price before the struck-through MRP.
    assert parsing.parse_price("₹11,089 ₹16,995 34% off") == 11089


# ----------------------------------------------------------- clean_title ----


def test_clean_title_strips_more_expander():
    assert parsing.clean_title("INVICTA Pro Diver Automatic  - For Men 30...more") == (
        "INVICTA Pro Diver Automatic - For Men 30"
    )


def test_clean_title_keeps_normal_titles():
    assert parsing.clean_title("INVICTA Pro Diver Automatic") == "INVICTA Pro Diver Automatic"


# ------------------------------------------------------------- ignoring -----

# Real titles from the eight listings that alerted on 2026-08-10.
IGNORED_TITLES = [
    "TIMEX Automatic Silver Dial Analog Watch - For Men TWEG208SMU04",
    "TIMEX Automatic Blue Dial Analog Watch - For Men TWEG208SMU07",
    "TIMEX Automatic Green Dial Analog Watch - For Men TWEG208SMU12",
    "TIMEX Automatic Black Dial Analog Watch - For Men TWEG208SMU05",
]
KEPT_TITLES = [
    # TW000Z801 used to live here: on 10 Aug only TWEG208 was muted, so its
    # sibling stayed active. On 11 Aug the whole TW000Z8 series was muted by
    # request, so it moved to test_movement.py's muted list.
    "TIMEX Automatic, Blue Dial Analog Watch - For Men TWEG286SMU03",
    "INVICTA 28948 Pro Diver Automatic Black Dial Analog Watch - For Men",
]


@pytest.mark.parametrize("title", IGNORED_TITLES)
def test_ignored_series_is_muted(title):
    assert parsing.is_ignored(title) is True


@pytest.mark.parametrize("title", KEPT_TITLES)
def test_other_models_still_alert(title):
    """TW000Z801 and TWEG286 are different series and must keep alerting."""
    assert parsing.is_ignored(title) is False


def test_ignore_matches_the_url_slug_too():
    assert parsing.is_ignored("", "https://shop.example.com/timex-tweg208smu04-automatic-x/p/y")


def test_ignore_is_case_insensitive():
    assert parsing.is_ignored("timex tweg208smu01 automatic")


def test_ignored_listing_never_becomes_a_candidate():
    # Only 1 of the 8 real cards carried the code, so this is the cheap path;
    # the product-page check in _verify_product is what catches the rest.
    listings = [
        L("a", "TIMEX TWEG208SMU04 Automatic Silver Dial", 6369),
        L("b", "TIMEX Automatic Blue Dial", 6159, "https://shop.example.com/timex-tweg208smu07-automatic/p/b"),
        L("c", "INVICTA Pro Diver Automatic", 7499),
    ]
    assert [x.pid for x in parsing.select_candidates(listings, 8000)] == ["c"]


# --------------------------------------------------------- movement spec ----

SPEC_TEXT = """General
Brand
TIMEX
Movement
Mechanical Automatic
Water Resistance Depth
50 m"""

# A real review that tripped the old any-line-containing-'movement' matcher.
REVIEW_TEXT = """Ratings & Reviews
Loved it..nh70 tmi movement 🥰
Manish Kadel"""


def test_extract_movement_reads_the_spec_row():
    assert parsing.extract_movement(SPEC_TEXT) == "Mechanical Automatic"


def test_extract_movement_handles_type_of_movement_label():
    assert parsing.extract_movement("Type of Movement\nQuartz\n") == "Quartz"


def test_extract_movement_ignores_reviews_mentioning_movement():
    """The old matcher returned 'Loved it..nh70 tmi movement 🥰' as the spec,
    which made the automatic-vs-quartz guard meaningless."""
    assert parsing.extract_movement(REVIEW_TEXT) is None


def test_extract_movement_returns_none_when_absent():
    assert parsing.extract_movement("No specifications rendered") is None


# ------------------------------------------------------------ stock read ----

# Real text from the unbuyable ₹8,349 Timex (itm37414b076f0fd). Note its
# JSON-LD simultaneously claimed schema.org/InStock.
TIMEX_OOS_TEXT = """Key Highlights
+7
Selected Strap Color:
Silver

Out of stock
Selected Dial Color:
Blue

Out of stock
Visit brand store
TIMEX Automatic, Blue Dial Analog Watch"""

# A watch that is buyable, but whose *other* colour swatch is sold out. The
# naive substring check marked this unavailable and silently dropped the deal.
OTHER_VARIANT_OOS_TEXT = """Selected Dial Color:
Blue

Add to cart
More colours
Green
Out of stock"""


def test_stock_detects_selected_variant_sold_out():
    in_stock, why = parsing.determine_stock(TIMEX_OOS_TEXT, ld_in_stock=True)
    assert in_stock is False
    assert "selected variant" in why


def test_stock_ignores_a_sold_out_sibling_variant():
    """The regression this guards: 'Out of stock' anywhere on the page used to
    mean skip, so a buyable watch with one sold-out colour vanished silently."""
    in_stock, _ = parsing.determine_stock(OTHER_VARIANT_OOS_TEXT, ld_in_stock=True)
    assert in_stock is True


def test_stock_trusts_json_ld_when_page_text_is_quiet():
    assert parsing.determine_stock("Add to cart", ld_in_stock=True)[0] is True
    assert parsing.determine_stock("Add to cart", ld_in_stock=False)[0] is False


def test_stock_catches_currently_unavailable():
    in_stock, why = parsing.determine_stock("This item is currently unavailable", True)
    assert in_stock is False and "unavailable" in why


def test_selected_variant_matcher_needs_proximity():
    # 'Out of stock' far away from the Selected label is a different variant.
    far = "Selected Dial Color:\nBlue\n" + ("filler line\n" * 12) + "Out of stock"
    assert parsing.selected_variant_out_of_stock(far) is False


# -------------------------------------------------------- movement filter ---


def test_looks_automatic_matches_title():
    assert parsing.looks_automatic("INVICTA Pro Diver Automatic Black Dial Analog Watch")


def test_looks_automatic_matches_url_slug():
    assert parsing.looks_automatic("", "https://shop.example.com/invicta-30091-pro-diver-automatic-black/p/x")


def test_looks_automatic_rejects_quartz_and_digital():
    assert not parsing.looks_automatic("Activa By Invicta Black Dial Digital Watch - For Men")
    assert not parsing.looks_automatic("Celestial Quartz Blue Dial Analog Watch - For Men 5046")


def test_brand_of_matches_each_watched_brand():
    assert parsing.brand_of("INVICTA Pro Diver Automatic") == "invicta"
    assert parsing.brand_of("Casio Enticer Automatic Analog Watch") == "casio"
    assert parsing.brand_of("Timex Marlin Automatic") == "timex"


def test_brand_of_reads_the_url_when_the_card_title_is_truncated():
    assert parsing.brand_of("", "https://shop.example.com/casio-mtp-automatic-x/p/y") == "casio"


def test_brand_of_rejects_unwatched_brands():
    assert parsing.brand_of("Seiko 5 Automatic") is None
    assert parsing.brand_of("Fossil Townsman Automatic") is None


def test_brand_of_is_word_bounded():
    # A substring inside a model code must not count as a brand hit.
    assert parsing.brand_of("Generic TIMEXA-900 Automatic") is None
    assert parsing.brand_of("Generic CASIOTRON Automatic") is None


# ------------------------------------------------------ select_candidates ---


def test_select_candidates_filters_brand_and_movement():
    listings = [
        L("a", "INVICTA Pro Diver Automatic", 7499),          # keep
        L("b", "INVICTA Pro Diver Automatic", 90000),         # way over the margin
        L("c", "Activa By Invicta Digital Watch", 1129),      # not automatic
        L("d", "Seiko 5 Sports Automatic", 6500),             # unwatched brand
    ]
    assert [x.pid for x in parsing.select_candidates(listings, 8000)] == ["a"]


def test_select_candidates_accepts_all_three_brands():
    listings = [
        L("a", "INVICTA Pro Diver Automatic", 7499),
        L("b", "Casio Enticer Automatic Analog Watch", 5999),
        L("c", "Timex Automatic Analog Watch", 7200),
    ]
    assert sorted(x.pid for x in parsing.select_candidates(listings, 8000)) == ["a", "b", "c"]


def test_select_candidates_keeps_unpriced_cards_for_verification():
    # A card whose price we failed to parse must still get a product-page check.
    picked = parsing.select_candidates([L("e", "INVICTA Pro Diver Automatic", None)], 8000)
    assert [x.pid for x in picked] == ["e"]


def test_select_candidates_allows_margin_above_threshold():
    # Card price is advisory; JSON-LD decides. 8000 * 1.5 = 12000 ceiling.
    assert len(parsing.select_candidates([L("a", "INVICTA Automatic", 11000)], 8000)) == 1
    assert parsing.select_candidates([L("a", "INVICTA Automatic", 12001)], 8000) == []


# --------------------------------------------------- extract_ld_product -----

PRODUCT_LD = json.dumps(
    [
        {
            "@type": "Product",
            "name": "INVICTA 28949 Automatic Blue Dial Analog Watch  - For Men",
            "offers": {
                "@type": "Offer",
                "price": 11699,
                "priceCurrency": "INR",
                "availability": "https://schema.org/InStock",
            },
        }
    ]
)


def test_extract_ld_product_reads_price_and_stock():
    got = parsing.extract_ld_product([PRODUCT_LD])
    assert got["price"] == 11699
    assert got["in_stock"] is True
    assert got["name"] == "INVICTA 28949 Automatic Blue Dial Analog Watch - For Men"


def test_extract_ld_product_ignores_carousel_prices():
    """The regression that started this: a recommendation carousel on the page
    showed ₹2,699 with byte-identical CSS classes to the real price element.
    JSON-LD describes only the product itself, so the trap cannot fire."""
    page_text = "₹2,699 ₹11,039 ₹23,375"  # what the DOM/text scrape would see
    got = parsing.extract_ld_product([PRODUCT_LD])
    assert got["price"] == 11699
    assert parsing.parse_price(page_text) == 2699  # the old, wrong answer


def test_extract_ld_product_skips_non_product_nodes():
    breadcrumb = json.dumps({"@type": "BreadcrumbList", "itemListElement": []})
    assert parsing.extract_ld_product([breadcrumb]) is None
    assert parsing.extract_ld_product([breadcrumb, PRODUCT_LD])["price"] == 11699


def test_extract_ld_product_detects_out_of_stock():
    ld = json.dumps(
        {
            "@type": "Product",
            "name": "x",
            "offers": {"price": 5000, "availability": "https://schema.org/OutOfStock"},
        }
    )
    assert parsing.extract_ld_product([ld])["in_stock"] is False


def test_extract_ld_product_handles_string_and_float_prices():
    for raw, expected in [("7499", 7499), (7499.0, 7499), ("7499.00", 7499)]:
        ld = json.dumps({"@type": "Product", "name": "x", "offers": {"price": raw}})
        assert parsing.extract_ld_product([ld])["price"] == expected


def test_extract_ld_product_survives_garbage():
    assert parsing.extract_ld_product([]) is None
    assert parsing.extract_ld_product(["not json {{{"]) is None
    assert parsing.extract_ld_product([json.dumps({"@type": "Product", "name": "x"})])["price"] is None


# -------------------------------------------------------------- new_pids ----


def test_new_pids_reports_unseen_only():
    listings = [L("a", "t", 1), L("b", "t", 2)]
    assert parsing.new_pids(listings, set()) == {"a", "b"}
    assert parsing.new_pids(listings, {"a"}) == {"b"}
    assert parsing.new_pids(listings, {"a", "b"}) == set()


# --------------------------------------------------------- decide_alerts ----


# ------------------------------------------------------------- backoff ------


def test_next_backoff_doubles_and_caps():
    assert runstate.next_backoff(0) == 0
    assert runstate.next_backoff(1) == 600
    assert runstate.next_backoff(2) == 1200
    assert runstate.next_backoff(3) == 2400
    assert runstate.next_backoff(50) == config.BACKOFF_MAX_SEC


def test_record_failure_accumulates_and_schedules():
    state = runstate.record_failure({}, now=1000.0, reason="network unreachable")
    assert state["failures"] == 1
    assert state["backoff_until_ts"] == 1600.0
    state = runstate.record_failure(state, now=1600.0, reason="network unreachable")
    assert state["failures"] == 2
    assert state["backoff_until_ts"] == 1600.0 + 1200


def test_record_success_clears_backoff():
    failed = runstate.record_failure({}, now=1000.0, reason="x")
    ok = runstate.record_success(failed, now=2000.0)
    assert ok["failures"] == 0
    assert ok["backoff_until_ts"] == 0
    assert ok["last_success_ts"] == 2000.0
    assert ok["last_failure"] is None


# ------------------------------------------------------------ throttle ------


def test_should_run_when_never_run_before():
    go, _ = runstate.should_run(now=1000.0, state={}, min_interval=540)
    assert go


def test_should_run_throttles_a_burst_of_catch_up_ticks():
    # launchd fires the missed run on wake; a second tick 30s later must not scrape.
    state = {"last_attempt_ts": 1000.0}
    assert runstate.should_run(1030.0, state, 540)[0] is False
    assert runstate.should_run(1541.0, state, 540)[0] is True


def test_should_run_respects_backoff_even_when_due():
    state = {"last_attempt_ts": 0.0, "backoff_until_ts": 5000.0, "failures": 2}
    go, why = runstate.should_run(4000.0, state, 540)
    assert not go and "backing off" in why
    assert runstate.should_run(5001.0, state, 540)[0] is True


def test_force_overrides_both_guards():
    state = {"last_attempt_ts": 999.0, "backoff_until_ts": 1e12, "failures": 9}
    go, why = runstate.should_run(1000.0, state, 540, force=True)
    assert go and why == "forced"


def test_record_attempt_is_stamped_before_work():
    state = runstate.record_attempt({"alerted": {"a": 1}}, now=1234.0)
    assert state["last_attempt_ts"] == 1234.0
    assert state["alerted"] == {"a": 1}


# ------------------------------------------------------ lid / interval ------


def test_lid_closed_slows_the_cadence():
    assert runstate.effective_min_interval(540, lid_closed=False) == 540
    assert runstate.effective_min_interval(540, lid_closed=True) == 1620


# ------------------------------------------------------------- network ------


def test_classify_probe_accepts_a_fast_link():
    usable, reason = runstate.classify_probe(True, 0.05)
    assert usable and "0.05" in reason


def test_classify_probe_rejects_unreachable():
    usable, reason = runstate.classify_probe(False, None)
    assert not usable and reason == "unreachable"


def test_classify_probe_rejects_insufficient_link():
    # Reachable but crawling: Chromium would time out, so don't spend the scrape.
    usable, reason = runstate.classify_probe(True, 4.5)
    assert not usable and "too slow" in reason


def test_classify_probe_boundary_is_inclusive():
    assert runstate.classify_probe(True, config.NET_SLOW_SEC)[0] is True
    assert runstate.classify_probe(True, config.NET_SLOW_SEC + 0.01)[0] is False


def test_wait_for_network_retries_until_the_link_comes_up(monkeypatch):
    """The wake-from-sleep case: launchd fires before Wi-Fi has associated."""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return (True, 0.04) if attempts["n"] >= 3 else (False, None)

    monkeypatch.setattr(runstate, "probe_network", flaky)
    ok, reason = runstate.wait_for_network(retries=5, delay=0)
    assert ok and attempts["n"] == 3


def test_wait_for_network_gives_up_when_offline(monkeypatch):
    monkeypatch.setattr(runstate, "probe_network", lambda: (False, None))
    ok, reason = runstate.wait_for_network(retries=3, delay=0)
    assert not ok and reason == "unreachable"


def test_wait_for_network_gives_up_on_a_permanently_slow_link(monkeypatch):
    monkeypatch.setattr(runstate, "probe_network", lambda: (True, 9.0))
    ok, reason = runstate.wait_for_network(retries=2, delay=0)
    assert not ok and "too slow" in reason


# ---------------------------------------------------------- mobile push -----


def test_ntfy_payload_carries_title_message_and_click():
    p = notify.build_ntfy_payload("t", "⌚ Timex ₹7,499", "body", "https://example.com/x")
    assert p["topic"] == "t"
    assert p["title"] == "⌚ Timex ₹7,499"
    assert p["click"] == "https://example.com/x"
    assert p["priority"] == 4


def test_ntfy_payload_omits_click_when_there_is_no_url():
    assert "click" not in notify.build_ntfy_payload("t", "a", "b")


def test_ntfy_payload_survives_json_encoding_with_rupee():
    """Sent as a JSON body precisely because ntfy's header API is latin-1 and
    every alert title contains ₹."""
    raw = json.dumps(notify.build_ntfy_payload("t", "₹7,499 ⌚", "₹ body"))
    assert json.loads(raw)["title"] == "₹7,499 ⌚"
    with pytest.raises(UnicodeEncodeError):
        "₹7,499".encode("latin-1")  # what the header API would have attempted


def test_ntfy_topic_prefers_env_over_file(tmp_path, monkeypatch):
    f = tmp_path / "topic.txt"
    f.write_text("from-file\n")
    monkeypatch.setattr(config, "NTFY_TOPIC_FILE", f)
    monkeypatch.setenv("NTFY_TOPIC", "from-env")
    assert notify.ntfy_topic() == "from-env"


def test_ntfy_topic_reads_the_file_when_env_is_empty(tmp_path, monkeypatch):
    """launchd passes a bare environment, so the file is the path that actually
    matters for scheduled runs."""
    f = tmp_path / "topic.txt"
    f.write_text("  from-file \n")
    monkeypatch.setattr(config, "NTFY_TOPIC_FILE", f)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notify.ntfy_topic() == "from-file"


def test_ntfy_topic_none_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC_FILE", tmp_path / "missing.txt")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notify.ntfy_topic() is None


def test_ntfy_is_unavailable_without_a_topic(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "NTFY_TOPIC_FILE", tmp_path / "missing.txt")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    channel = notify.NtfyNotifier()
    assert channel.available() is False
    assert channel.send("t", "m") is False


def test_ntfy_swallows_network_errors(monkeypatch):
    """A dead phone channel must not fail the check or trip the backoff."""
    monkeypatch.setenv("NTFY_TOPIC", "t")
    import urllib.request

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert notify.NtfyNotifier().send("t", "m") is False


# ------------------------------------------------------------- notifier -----


def test_find_notifier_prefers_path():
    assert notify.find_notifier(which=lambda _: "/somewhere/terminal-notifier") == (
        "/somewhere/terminal-notifier"
    )


def test_find_notifier_falls_back_for_launchd():
    """launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin — Homebrew is invisible,
    so the scheduled job must still find terminal-notifier by absolute path."""
    got = notify.find_notifier(
        which=lambda _: None,
        exists=lambda p: p == "/opt/homebrew/bin/terminal-notifier",
    )
    assert got == "/opt/homebrew/bin/terminal-notifier"


def test_find_notifier_returns_none_when_absent():
    # Falls back to osascript banners; alerts still fire, just not clickable.
    assert notify.find_notifier(which=lambda _: None, exists=lambda _: False) is None


# -------------------------------------------------------------- locking -----


def test_single_instance_excludes_a_second_runner(tmp_path, monkeypatch):
    # Must not share the live lock file: the scheduled job holds it during a
    # real check, which would fail this test for no good reason.
    lock = tmp_path / "test.lock"
    monkeypatch.setattr(config, "LOCK_FILE", lock)
    with runstate.single_instance() as first:
        assert first is True
        # Simulate the next launchd tick arriving mid-scrape.
        import subprocess as sp

        probe = sp.run(
            [
                sys.executable,
                "-c",
                "import sys,pathlib;"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r});"
                "from watchmon import config, runstate;"
                f"config.LOCK_FILE=pathlib.Path({str(lock)!r});"
                "ctx=runstate.single_instance();print('GOT' if ctx.__enter__() else 'BLOCKED')",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "BLOCKED" in probe.stdout, probe.stdout + probe.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
