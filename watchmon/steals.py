"""Deciding what counts as a steal.

Pure: give it a price and that product's history, it answers. No I/O, so the
rule is testable without a database or a network.

The rule is deliberately conservative. A price monitor that cries wolf gets
muted, and a muted monitor is worth nothing — so a steal must clear *both* a
relative-discount bar and an all-time-low bar, and only once the product has
enough history for those numbers to mean anything.
"""

from __future__ import annotations

from . import config
from .models import PriceStats


def is_steal(price: int | None, stats: PriceStats) -> tuple[bool, str]:
    """Return (is_steal, human-readable reason).

    The reason is returned in both branches so the log explains near-misses,
    not just hits — otherwise "why didn't it alert?" is unanswerable.
    """
    if price is None:
        return False, "no price"

    if price < config.STEAL_MIN_PRICE:
        return False, f"₹{price:,} below the ₹{config.STEAL_MIN_PRICE:,} floor (percentages are noise down here)"

    if stats.days < config.STEAL_MIN_HISTORY_DAYS:
        return False, f"only {stats.days} day(s) of history, need {config.STEAL_MIN_HISTORY_DAYS}"

    if not stats.has_enough_history or stats.median is None or stats.min_ever is None:
        return False, "no usable history"

    discount = 1 - (price / stats.median)
    if discount < config.STEAL_DISCOUNT:
        return False, f"₹{price:,} is {discount:.0%} off its ₹{stats.median:,} median, need {config.STEAL_DISCOUNT:.0%}"

    ceiling = stats.min_ever * config.STEAL_NEAR_LOW_RATIO
    if price > ceiling:
        return False, f"₹{price:,} above its ₹{stats.min_ever:,} all-time low"

    return True, (
        f"{discount:.0%} below its ₹{stats.median:,} median "
        f"({stats.days}d history, previous low ₹{stats.min_ever:,})"
    )


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
