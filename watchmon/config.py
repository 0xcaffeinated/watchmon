"""All tunables in one place.

Values live at module level (not in a class) so tests can monkeypatch a single
attribute without constructing anything. Everything that varies per run — the
price threshold above all — is passed as an argument instead, never mutated
here; the old code mutated a module global and let two callers disagree.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------- brands ----

# Watched for BOTH features: the ₹8,000 automatic rule and price history.
BRANDS = ("invicta", "casio", "timex", "alba")

# Model families to never alert on, matched case-insensitively against title,
# page heading and URL.
#
#   TWEG208   Timex automatics that sat at ₹6,159–₹7,309 (muted 10 Aug).
#   TW000Z8   Timex automatics TW000Z800/801/802 — the beige/black/copper trio
#             that alerted at ₹7,369–₹7,509 (muted 11 Aug). Note this covers
#             TW000Z801, which had deliberately been left active earlier.
#
# These codes usually appear ONLY in the product page h1 — not in the JSON-LD
# name and not in the URL slug — so matching happens after the page fetch.
IGNORE_PATTERNS = (r"TWEG208", r"TW000Z8")

# --------------------------------------------------- the ₹8,000 automatic ---

THRESHOLD_INR = 8000

# Out-of-stock listings are dropped rather than labelled (changed 17 Aug).
# Note the trade-off this accepts: the stock signals genuinely disagree — an
# unbuyable Timex advertised schema.org/InStock — so a false "out of stock"
# now silently costs a real deal. Every drop is logged and counted in the daily
# report so it stays visible; flip this back to True to alert with a label.
ALERT_ON_OUT_OF_STOCK = False

# How far above the threshold a search-card price may sit and still earn a
# product-page check. Absorbs card-parse error.
VERIFY_MARGIN = 1.5

# Verifying a candidate costs a page load (~3s); the run must fit the schedule.
MAX_VERIFY_PER_RUN = 40

# ------------------------------------------------------ steal detection -----

# A steal is a watch cheap *relative to its own past*, not to a the site MRP.
# Both conditions must hold, and only after enough history exists.
STEAL_DISCOUNT = 0.40  # >= 40% below its 30-day median
STEAL_MEDIAN_WINDOW_DAYS = 30
STEAL_MIN_HISTORY_DAYS = 14  # distinct days before a median means "normal"
STEAL_NEAR_LOW_RATIO = 1.02  # within 2% of the cheapest ever seen
# Below this, percentages are noise and the hits are all cheap quartz: at a
# ₹1,500 floor, every one of the 16-20 dips in 10 days was a sub-₹4,000 Casio.
STEAL_MIN_PRICE = 4000

# ----------------------------------------------------------------- paths ----

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state.json"
LOCK_FILE = ROOT / "monitor.lock"
HISTORY_DB = ROOT / "history.db"
LOG_FILE = ROOT / "logs" / "monitor.log"
DEALS_FILE = ROOT / "logs" / "deals.jsonl"
NTFY_TOPIC_FILE = ROOT / "ntfy_topic.txt"

# ---------------------------------------------------------------- search ----

# The storefront is configuration, never a literal: this repo is public, and
# the target does not need to be advertised in it. Set WATCH_SITE_BASE, or put
# the origin in site.txt (gitignored) for local runs.
def _resolve_site_base() -> str:
    from_env = os.environ.get("WATCH_SITE_BASE", "").strip()
    if from_env:
        return from_env.rstrip("/")
    try:
        return (ROOT / "site.txt").read_text().strip().rstrip("/")
    except OSError:
        return ""


# One search per brand. `{query}` is the brand plus any qualifier.
SEARCH_PATH = "/search?q={query}&sort=price_asc&page={page}"

SITE_BASE = _resolve_site_base()
# The ₹8,000 rule only cares about automatics, so its sweep stays narrow.
AUTOMATIC_QUERY = "{brand}+automatic"
# History wants every watch of the brand, so its sweep is wide.
HISTORY_QUERY = "{brand}+watch"

# NB: the site's price_asc is NOT monotonic across pages (observed: page 3 ran
# to ₹58,220, page 4 restarted at ₹6,500), so we never stop early on price.
MAX_PAGES_AUTOMATIC = 6
MAX_PAGES_HISTORY = 8

# An empty page used to mean "end of results". It usually means the site just
# throttled us: alba/auto truncated at page 2-3 sixteen times in 24h while the
# brand really has 160+ listings. Retry once before believing it.
PAGE_RETRY_ATTEMPTS = 1
PAGE_RETRY_DELAY_SEC = 6.0
# Pace between pages, to be less worth throttling in the first place.
PAGE_PAUSE_SEC = 1.6
# Wait for the first product card instead of sleeping a flat interval.
CARD_WAIT_MS = 12_000

# The wide history sweep costs ~2.5 min of the ~4 min total, and history is
# bucketed by day anyway — running it every tick would burn battery and hammer
# The site to refine a number that only changes once a day. The narrow
# automatic sweep still runs every tick, so the ₹8,000 rule stays responsive.
HISTORY_INTERVAL_SEC = 2 * 3600

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ------------------------------------------------------------ scheduling ----

MIN_INTERVAL_SEC = 540
LID_CLOSED_MULTIPLIER = 3
BACKOFF_BASE_SEC = 600
BACKOFF_MAX_SEC = 3 * 3600

# -- wake handling -----------------------------------------------------------
# launchd fires the catch-up run the instant the machine wakes. During a dark
# wake the TCP probe answers in ~60ms while real page loads still time out, so
# those attempts failed and compounded the backoff to 51 minutes — which then
# blocked the check the user was present for. Failures this soon after a wake
# are treated as transient: retried shortly, never escalated.
WAKE_GRACE_SEC = 180
WAKE_RETRY_SEC = 300
# If the user has touched the machine this recently, they are present and a
# check should not be withheld for a backoff earned while it was asleep.
USER_PRESENT_SEC = 120

# ------------------------------------------------------- network health -----

NET_PROBE_HOST = urlparse(SITE_BASE).hostname or ""
NET_PROBE_PORT = 443
NET_CONNECT_TIMEOUT = 5.0
NET_SLOW_SEC = 3.0
NET_RETRIES = 6
NET_RETRY_DELAY_SEC = 10

# ---------------------------------------------------------- notification ----

NTFY_SERVER = "https://ntfy.sh"
NTFY_TIMEOUT_SEC = 10
NOTIFIER_PATHS = (
    "/opt/homebrew/bin/terminal-notifier",  # Apple silicon
    "/usr/local/bin/terminal-notifier",  # Intel
)
