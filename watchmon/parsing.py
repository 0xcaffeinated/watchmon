"""Pure parsing and filtering. No network, no disk, no clock.

Each rule here exists because the obvious version was wrong against the real
site; the comments record which failure produced which rule.
"""

from __future__ import annotations

import json
import re

from . import config
from .models import Listing

PRICE_RE = re.compile(r"₹\s?([\d,]+)")

# Confirms a movement spec really is self-winding.
AUTOMATIC_RE = re.compile(r"automatic|self[\s-]?wind", re.IGNORECASE)
# Hand-wound mechanicals are NOT automatics.
MANUAL_RE = re.compile(r"hand[\s-]?wind|manual[\s-]?wind", re.IGNORECASE)
# Card-level hint. Must include "mechanical": the site lists Alba's automatics
# as "Mechanical" and never "Automatic" (13 of them, ₹9,450–₹16,200), so an
# automatic-only pre-filter made the entire brand invisible. The movement spec
# on the product page is what actually decides.
AUTOMATIC_HINT_RE = re.compile(r"automatic|mechanical|self[\s-]?wind", re.IGNORECASE)


def parse_price(text: str) -> int | None:
    """First rupee amount in a blob of card text. None if absent."""
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def clean_title(raw: str) -> str:
    """The listing h1 carries a '...more' expander; strip it, collapse space."""
    title = (raw or "").strip().split("\n")[0]
    title = re.sub(r"\s*\.\.\.\s*more$|\s*…\s*more$", "", title, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", title).strip()


def looks_automatic(title: str, url: str = "") -> bool:
    """Cheap pre-filter on the search card. Deliberately permissive.

    Accepts "mechanical" as well as "automatic" — see AUTOMATIC_HINT_RE. Some
    Alba cards carry the word only in the URL slug, never in the title, which
    is why the slug is searched too.
    """
    return bool(AUTOMATIC_HINT_RE.search(f"{title} {url}"))


def is_automatic_movement(movement: str | None, title: str) -> tuple[bool, str]:
    """Decide from the product page whether this is genuinely self-winding.

    The spec table wins when present. "Mechanical Automatic" is the site's
    value for a real automatic; a hand-wound piece says "Hand Winding", and
    letting the permissive card filter through unchecked would alert on those.
    """
    if movement:
        if AUTOMATIC_RE.search(movement):
            return True, movement
        if MANUAL_RE.search(movement):
            return False, f"movement spec says {movement!r} (hand-wound, not automatic)"
        return False, f"movement spec says {movement!r}"

    # No spec rendered: fall back to the title, but only on an explicit
    # "automatic" — a bare "Mechanical" title is ambiguous.
    if AUTOMATIC_RE.search(title):
        return True, "(inferred from title; no spec on page)"
    return False, "no movement spec and title is not explicitly automatic"


def brand_of(title: str, url: str = "") -> str | None:
    """Which watched brand a listing belongs to, or None.

    Word-bounded so a model code can't match (TIMEXA-900 is not a Timex), and
    checked against the URL slug too because cards truncate titles.
    """
    haystack = f"{title} {url}".lower()
    for brand in config.BRANDS:
        if re.search(rf"\b{re.escape(brand)}\b", haystack):
            return brand
    return None


def is_ignored(title: str, url: str = "") -> bool:
    """True for muted model families (config.IGNORE_PATTERNS)."""
    haystack = f"{title} {url}"
    return any(re.search(p, haystack, re.IGNORECASE) for p in config.IGNORE_PATTERNS)


def extract_movement(text: str) -> str | None:
    """Read movement from the specifications table.

    Anchored on a standalone 'Movement' label: matching any line *containing*
    the word scraped customer reviews instead ("Loved it..nh70 tmi movement"),
    which made the automatic-vs-quartz guard meaningless.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    for i, line in enumerate(lines):
        if not re.fullmatch(r"(type of )?movement", line, re.IGNORECASE):
            continue
        for value in lines[i + 1 :]:
            if value:
                return value[:80]
    return None


def selected_variant_out_of_stock(text: str) -> bool:
    """True when the *selected* colour/strap variant is unavailable.

    The site prints 'Out of stock' against every sold-out swatch, so a bare
    substring search marks a watch unavailable whenever ANY variant is gone.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    for i, line in enumerate(lines):
        if not re.match(r"Selected\b.*:", line, re.IGNORECASE):
            continue
        following = [ln for ln in lines[i + 1 :] if ln][:2]
        if any(re.fullmatch(r"out of stock|sold out", ln, re.IGNORECASE) for ln in following):
            return True
    return False


def determine_stock(page_text: str, ld_in_stock: bool) -> tuple[bool, str]:
    """Best-effort stock read. Only ever labels — never suppresses an alert.

    The signals disagree in practice: an unbuyable Timex still advertised
    schema.org/InStock. A wrong 'out of stock' silently costs a deal; a wrong
    'in stock' costs one dismissible banner.
    """
    if selected_variant_out_of_stock(page_text):
        return False, "selected variant out of stock"
    if re.search(r"currently unavailable", page_text or "", re.IGNORECASE):
        return False, "listing unavailable"
    if not ld_in_stock:
        return False, "JSON-LD says out of stock"
    return True, "in stock"


def extract_ld_product(blobs: list[str]) -> dict | None:
    """Pull the Product node out of a page's JSON-LD scripts.

    The source of truth for price. The rendered DOM is not: recommendation
    carousels use byte-identical class names to the real price element, so
    'first ₹ on the page' returned ₹2,699 for an ₹11,699 watch.
    """
    for blob in blobs:
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            try:
                price = int(float(offers.get("price")))
            except (TypeError, ValueError):
                price = None
            availability = str(offers.get("availability") or "")
            return {
                "name": clean_title(node.get("name") or ""),
                "price": price,
                "in_stock": "outofstock" not in availability.lower().replace("_", ""),
            }
    return None


def parse_pager_total(text: str) -> int | None:
    """Total page count from the site's 'Page 2 of 3' footer, if present.

    Without this the sweep discovers the end by requesting a page that does not
    exist and getting nothing back — which is indistinguishable from being
    throttled, so every completed sweep logged a truncation warning.
    """
    match = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text or "", re.IGNORECASE)
    return int(match.group(2)) if match else None


def new_pids(listings: list[Listing], seen: set[str]) -> set[str]:
    """Product ids on this page we haven't already collected."""
    return {x.pid for x in listings} - seen


def watched_listings(listings: list[Listing]) -> list[Listing]:
    """Listings belonging to a watched brand and not muted. For history."""
    kept = []
    for item in listings:
        brand = brand_of(item.title, item.url)
        if brand is None or is_ignored(item.title, item.url):
            continue
        item.brand = brand
        kept.append(item)
    return kept


def select_candidates(listings: list[Listing], threshold: int) -> list[Listing]:
    """Automatics worth opening the product page for, for the ₹8,000 rule.

    The gate is loose on purpose (VERIFY_MARGIN above threshold, and unpriced
    cards still checked): a card-parse slip must never cost a real deal.
    """
    ceiling = threshold * config.VERIFY_MARGIN
    out = []
    for item in watched_listings(listings):
        if not looks_automatic(item.title, item.url):
            continue
        if item.price is not None and item.price > ceiling:
            continue
        out.append(item)
    return out
