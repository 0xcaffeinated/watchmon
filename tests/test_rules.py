#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""The rule engine: a product category is data, not code.

The point of this file is that a brand-new category can be expressed without
touching any module, so each test below uses a category the tool has never
scraped.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchmon import config, parsing  # noqa: E402
from watchmon.models import Rule  # noqa: E402

KEYBOARDS = Rule(
    name="brown-switch keyboards",
    brands=("keychron", "logitech"),
    match_query="{brand}+mechanical+keyboard",
    history_query="{brand}+keyboard",
    include=r"mechanical",
    ceiling=6000,
    require_spec={r"switch type": r"brown"},
    reject_spec={r"switch type": r"blue|red"},
)

KEYBOARD_SPEC = """General
Brand
Keychron
Switch Type
Gateron Brown
Layout
75%"""


def test_queries_are_generated_per_brand():
    assert KEYBOARDS.queries() == [
        "keychron+mechanical+keyboard",
        "logitech+mechanical+keyboard",
    ]
    assert KEYBOARDS.queries(wide=True) == ["keychron+keyboard", "logitech+keyboard"]


def test_a_matching_card_is_accepted():
    assert parsing.matches_rule("Keychron K2 Mechanical Keyboard", "", KEYBOARDS)


def test_wrong_brand_is_rejected():
    assert not parsing.matches_rule("Corsair K70 Mechanical Keyboard", "", KEYBOARDS)


def test_missing_include_term_is_rejected():
    assert not parsing.matches_rule("Keychron B1 Slim Membrane Keyboard", "", KEYBOARDS)


def test_required_spec_must_match():
    ok, note = parsing.check_specs(KEYBOARD_SPEC, "Keychron K2", KEYBOARDS)
    assert ok, note


def test_rejected_spec_excludes_on_its_own():
    """A reject-only rule: nothing is required, but blue switches are out.
    (With KEYBOARDS the require_spec catches blue first, which is also correct
    — this exercises the exclusion path in isolation.)"""
    no_blue = Rule(name="any but blue", brands=("keychron",),
                   reject_spec={r"switch type": r"blue"})
    brown = parsing.check_specs(KEYBOARD_SPEC, "Keychron K2", no_blue)
    blue = KEYBOARD_SPEC.replace("Gateron Brown", "Gateron Blue")
    ok, note = parsing.check_specs(blue, "Keychron K2", no_blue)
    assert brown[0] is True
    assert not ok and "excluded by rule" in note


def test_wrong_spec_value_is_rejected():
    red = KEYBOARD_SPEC.replace("Gateron Brown", "Gateron Red")
    ok, _ = parsing.check_specs(red, "Keychron K2", KEYBOARDS)
    assert not ok


def test_absent_spec_falls_back_to_the_title():
    """No spec table rendered: an explicit title still establishes it, an
    ambiguous one does not."""
    ok, _ = parsing.check_specs("no specs here", "Keychron K2 Brown switches", KEYBOARDS)
    assert ok
    ok, note = parsing.check_specs("no specs here", "Keychron K2", KEYBOARDS)
    assert not ok and "does not establish" in note


def test_extract_spec_reads_any_label():
    assert parsing.extract_spec(KEYBOARD_SPEC, r"switch type") == "Gateron Brown"
    assert parsing.extract_spec(KEYBOARD_SPEC, r"layout") == "75%"
    assert parsing.extract_spec(KEYBOARD_SPEC, r"weight") is None


def test_per_rule_mute():
    muted = Rule(name="m", brands=("keychron",), include=r"mechanical", mute=(r"K2 ?Pro",))
    assert parsing.matches_rule("Keychron K8 Mechanical", "", muted)
    assert not parsing.matches_rule("Keychron K2 Pro Mechanical", "", muted)


# ------------------------------------------------- the shipped watch rule ----


def test_the_watch_rule_still_behaves_as_before():
    """The existing behaviour, now expressed as data rather than code."""
    rule = config.RULES[0]
    assert rule.ceiling == 8000

    auto = "Movement\nMechanical Automatic\n"
    hand = "Movement\nMechanical Hand Winding\n"
    quartz = "Movement\nQuartz\n"

    assert parsing.check_specs(auto, "INVICTA Pro Diver", rule)[0] is True
    assert parsing.check_specs(hand, "Some Mechanical", rule)[0] is False
    assert parsing.check_specs(quartz, "Whatever", rule)[0] is False

    # The card filter still accepts "Mechanical", which is how one brand lists
    # its automatics, and still rejects quartz/digital.
    assert parsing.matches_rule("ALBA AV3575X1 Mechanical Analog Watch", "", rule)
    assert parsing.matches_rule("INVICTA Pro Diver Automatic", "", rule)
    assert not parsing.matches_rule("CASIO Enticer Quartz Analog", "", rule)


def test_the_watch_rule_still_honours_global_mutes():
    rule = config.RULES[0]
    assert not parsing.matches_rule(
        "TIMEX Automatic Beige Dial Analog Watch TW000Z800", "", rule
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
