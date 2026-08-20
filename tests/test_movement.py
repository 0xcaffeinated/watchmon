#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Movement detection across the naming the site actually uses.

Regression origin: the site lists Alba's automatics as "Mechanical" and never
"Automatic". An automatic-only filter made all 13 of them invisible — the brand
looked like it had no automatics at all.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import parsing  # noqa: E402
from watchmon.models import Listing  # noqa: E402


def L(pid, title, price, url=""):
    return Listing(pid=pid, url=url or f"https://shop.example.com/x/p/{pid}", title=title, price=price)


# ------------------------------------------------------- the card filter ----


@pytest.mark.parametrize(
    "title",
    [
        "ALBA AV3575X1 Mechanical Analog Watch - For Men",
        "ALBA AL4321X1 Mechanical Analog Watch - For Men",
        "INVICTA Pro Diver Automatic Black Dial",
        "TIMEX Automatic Blue Dial Analog Watch",
    ],
)
def test_card_filter_accepts_automatic_and_mechanical(title):
    assert parsing.looks_automatic(title) is True


def test_card_filter_reads_the_slug_when_the_title_is_silent():
    """Real case: 'Alba Watches AL4251X1 Analog Watch' says neither word — only
    the URL slug carries 'mechanical'."""
    listing = L(
        "a",
        "Alba Watches AL4251X1 Analog Watch - For Men & Women",
        12150,
        "https://shop.example.com/alba-watches-al4251x1-mechanical-analog-watch-men/p/a",
    )
    assert parsing.looks_automatic(listing.title, listing.url) is True
    assert [x.pid for x in parsing.select_candidates([listing], 20000)] == ["a"]


@pytest.mark.parametrize(
    "title",
    [
        "Activa By Invicta Black Dial Digital Watch",
        "CASIO Enticer Quartz Analog Watch",
        "Alba Watches AH7CF0X1 Analog Watch - For Women",
    ],
)
def test_card_filter_still_rejects_quartz_and_digital(title):
    assert parsing.looks_automatic(title) is False


# ---------------------------------------------------- the spec confirmer ----


def test_mechanical_automatic_is_an_automatic():
    """The real spec value on every Alba automatic checked."""
    ok, note = parsing.is_automatic_movement("Mechanical Automatic", "ALBA AV3575X1 Mechanical")
    assert ok and note == "Mechanical Automatic"


def test_plain_automatic_spec_passes():
    assert parsing.is_automatic_movement("Automatic", "x")[0] is True


def test_self_winding_spec_passes():
    assert parsing.is_automatic_movement("Self-Winding", "x")[0] is True


def test_hand_wound_mechanical_is_rejected():
    """The cost of loosening the card filter: hand-wound pieces now reach the
    confirmer, and must not be alerted on."""
    ok, note = parsing.is_automatic_movement("Mechanical Hand Winding", "Some Mechanical Watch")
    assert not ok and "hand-wound" in note


def test_bare_mechanical_spec_is_not_assumed_automatic():
    ok, note = parsing.is_automatic_movement("Mechanical", "Some Mechanical Watch")
    assert not ok and "Mechanical" in note


def test_quartz_spec_is_rejected_even_if_the_card_slipped_through():
    assert parsing.is_automatic_movement("Quartz", "Mechanical-looking title")[0] is False


def test_missing_spec_falls_back_to_an_explicit_automatic_title():
    ok, note = parsing.is_automatic_movement(None, "TIMEX Automatic Blue Dial")
    assert ok and "inferred" in note


def test_missing_spec_will_not_infer_from_a_bare_mechanical_title():
    """A 'Mechanical' title with no spec is ambiguous — could be hand-wound."""
    assert parsing.is_automatic_movement(None, "ALBA AV3575X1 Mechanical Analog")[0] is False

# --------------------------------------------------------- muted series ----


@pytest.mark.parametrize(
    "heading",
    [
        "TIMEX Automatic Beige Dial Analog Watch - For Men TW000Z800",
        "TIMEX Automatic Black Dial Analog Watch - For Men TW000Z801",
        "TIMEX Automatic Copper Dial Analog Watch - For Men TW000Z802",
        "TIMEX Automatic Silver Dial Analog Watch - For Men TWEG208SMU04",
    ],
)
def test_muted_series_are_ignored(heading):
    assert parsing.is_ignored(heading) is True


@pytest.mark.parametrize(
    "heading",
    [
        "TIMEX Automatic, Blue Dial Analog Watch - For Men TWEG286SMU03",
        "INVICTA 28948 Pro Diver Automatic Black Dial Analog Watch - For Men",
        "ALBA AV3575X1 Mechanical Analog Watch - For Men",
    ],
)
def test_other_series_still_alert(heading):
    assert parsing.is_ignored(heading) is False


def test_muted_code_is_matched_from_the_heading_not_the_ld_name():
    """Both TW000Z8 listings carry the code only in the page h1 — the JSON-LD
    name and the URL slug are both silent, so card-level filtering can't see it."""
    ld_name = "TIMEX Automatic Copper Dial Analog Watch - For Men"
    heading = ld_name + " TW000Z802"
    url = "https://shop.example.com/timex-automatic-copper-dial-analog-watch-men/p/x"
    assert parsing.is_ignored(ld_name, url) is False          # what the card sees
    assert parsing.is_ignored(f"{ld_name} {heading}", url) is True  # what the page sees

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------------------------------------------------------- pagination ---


def test_pager_total_is_read():
    assert parsing.parse_pager_total('Showing 81 – 85 of 95 results\nPage 3 of 3') == 3
    assert parsing.parse_pager_total("Page 2 of 17") == 17


def test_pager_total_absent_is_none():
    """No pager: the sweep falls back to detecting the end by empty page."""
    assert parsing.parse_pager_total("no pager here") is None
    assert parsing.parse_pager_total("") is None
