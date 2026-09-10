"""Deciding what counts as a steal.

Pure: give it a price and that product's history, it answers. No I/O, so the
rule is testable without a database or a network.

The test is a discount against a 30-day baseline. A price monitor that cries
wolf gets muted, and a muted monitor is worth nothing, so the rule only applies
once the product has enough history for a baseline to mean anything — without
that guard every newly listed product is trivially its own all-time low and
alerts on sight.
"""

from __future__ import annotations

from . import config
from .models import PriceStats


def baseline_of(stats: PriceStats) -> tuple[int | None, str]:
    """The 30-day figure the discount is measured against, and its name."""
    if config.STEAL_BASELINE == "mean":
        return stats.mean, "average"
    return stats.median, "median"


def is_steal(price: int | None, stats: PriceStats) -> tuple[bool, str]:
    """Return (is_steal, human-readable reason).

    The reason is returned in both branches so the log explains near-misses,
    not just hits — otherwise "why didn't it alert?" is unanswerable.
    """
    if price is None:
        return False, "no price"

    if price < config.STEAL_MIN_PRICE:
        return False, (
            f"₹{price:,} below the ₹{config.STEAL_MIN_PRICE:,} floor "
            "(percentages are noise down here)"
        )

    if stats.days < config.STEAL_MIN_HISTORY_DAYS:
        return False, f"only {stats.days} day(s) of history, need {config.STEAL_MIN_HISTORY_DAYS}"

    baseline, label = baseline_of(stats)
    if not baseline:
        return False, "no usable history"

    discount = 1 - (price / baseline)
    if discount < config.STEAL_DISCOUNT:
        return False, (
            f"₹{price:,} is {discount:.0%} off its ₹{baseline:,} {label}, "
            f"need {config.STEAL_DISCOUNT:.0%}"
        )

    # Optional second bar: cheap against the baseline *and* at its lowest ever.
    # Off by default — the discount against the baseline is the whole test.
    if config.STEAL_REQUIRE_ALL_TIME_LOW:
        if stats.min_ever is None:
            return False, "no usable history"
        ceiling = stats.min_ever * config.STEAL_NEAR_LOW_RATIO
        if price > ceiling:
            return False, f"₹{price:,} above its ₹{stats.min_ever:,} all-time low"

    reason = f"{discount:.0%} below its ₹{baseline:,} {label} ({stats.days}d history"
    if stats.min_ever is not None:
        reason += f", previous low ₹{stats.min_ever:,}"
    return True, reason + ")"


def find_steals(
    priced: dict[str, int],
    stats: dict[str, PriceStats],
) -> list[tuple[str, str]]:
    """Screen many products at once. Returns [(pid, reason)] for steals only."""
    hits = []
    for pid, price in priced.items():
        ok, reason = is_steal(price, stats.get(pid, PriceStats()))
        if ok:
            hits.append((pid, reason))
    return hits
