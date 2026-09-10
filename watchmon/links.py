"""Affiliate link construction.

The network's own converter mints links of the form

    <redirector>/visitretailer/<retailer id>?id=<publisher id>&dl=<destination>

so a link can be built for any product without a manual conversion step. The
per-share code its tool adds is omitted; the redirector accepts links without
it.

Everything identifying — publisher id, retailer ids, the redirector host —
lives outside the repository, because this one is public and a publisher id is
an earnings identity: swapped in a fork, the commission follows the fork.
When nothing is configured every function degrades to the plain product URL,
so an unconfigured checkout still runs and simply earns nothing.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from . import config, sources

log = logging.getLogger("watchmon.links")


def settings() -> dict:
    """Affiliate configuration, or {} when unconfigured.

    Env first so CI can inject it as one secret; otherwise a gitignored file.
    """
    raw = os.environ.get("AFFILIATE_CONFIG", "").strip()
    if not raw:
        try:
            raw = config.AFFILIATE_FILE.read_text().strip()
        except OSError:
            return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("affiliate config is not valid JSON — links will be plain")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_configured() -> bool:
    conf = settings()
    return bool(conf.get("publisher_id") and conf.get("retailers"))


def affiliate_url(url: str, source_key: str | None = None) -> str:
    """Product URL -> profit link, or the URL unchanged if we cannot build one.

    Never raises and never returns something unclickable: a broken affiliate
    setup must degrade to an honest plain link, not to a dead post.
    """
    if not url:
        return url
    conf = settings()
    publisher = str(conf.get("publisher_id") or "")
    retailers = conf.get("retailers") or {}
    redirector = str(conf.get("redirector") or "").rstrip("/")
    if not publisher or not redirector:
        return url

    key = source_key or _source_key_for(url)
    retailer = str(retailers.get(key) or "")
    if not retailer:
        # A storefront with no retailer id mapped simply is not monetised.
        return url

    return f"{redirector}/visitretailer/{retailer}?id={publisher}&dl={quote(url, safe='')}"


def _source_key_for(url: str) -> str:
    for source in sources.SOURCES:
        if source.owns(url):
            return source.key
    return sources.LEGACY_KEY
