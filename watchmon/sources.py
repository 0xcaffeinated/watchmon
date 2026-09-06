"""Storefronts, as first-class objects.

A source knows three things a rule must not care about: how to reach a
storefront, how to sweep it, and how its URLs are stored.

That last one matters more than it sounds. The price database is committed to
a public repository, so a stored URL must never carry an origin. Each source
strips its own and tags the path with its key, and restores it on the way out.
Keeping that here rather than in the database layer is what stops a second
storefront quietly publishing its domain in every row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config

log = logging.getLogger("watchmon.sources")

# Legacy rows were written before there was more than one storefront and carry
# a bare "/path". They belong to the primary source.
LEGACY_KEY = "a"


@dataclass(frozen=True)
class Source:
    key: str
    kind: str  # "browser" | "json"
    max_pages: int

    @property
    def base(self) -> str:
        return config.SITE_BASE if self.key == "a" else config.SITE_B_BASE

    def enabled(self) -> bool:
        return bool(self.base)

    def open(self, headless: bool = True):
        """A context manager yielding something with sweep()/fetch_product()."""
        if self.kind == "browser":
            from .scraper import StoreScraper

            return StoreScraper(headless=headless)
        from .jsonstore import JsonStoreSource

        return JsonStoreSource(base=self.base)

    # ------------------------------------------------------------ storage ---

    def owns(self, url: str) -> bool:
        return bool(self.base) and (url or "").startswith(self.base)

    def relative(self, url: str) -> str:
        """Origin -> "<key>:<path>"."""
        return f"{self.key}:{(url[len(self.base):] or '/')}"

    def absolute(self, path: str) -> str:
        return self.base + path


SOURCES = (
    Source(key="a", kind="browser", max_pages=config.MAX_PAGES_AUTOMATIC),
    Source(key="b", kind="json", max_pages=config.MAX_PAGES_JSONSTORE),
)


def enabled() -> list[Source]:
    return [s for s in SOURCES if s.enabled()]


def by_key(key: str) -> Source | None:
    return next((s for s in SOURCES if s.key == key), None)


def store_url(url: str) -> str:
    """Strip whichever origin owns this URL before it is persisted."""
    for source in SOURCES:
        if source.owns(url):
            return source.relative(url)
    return url or ""


def restore_url(stored: str) -> str:
    """Inverse of store_url, tolerant of rows written before source keys."""
    stored = stored or ""
    if len(stored) > 2 and stored[1] == ":" and stored[0].isalpha():
        source = by_key(stored[0])
        if source and source.base:
            return source.absolute(stored[2:])
        return stored[2:]
    if stored.startswith("/"):
        source = by_key(LEGACY_KEY)
        return source.absolute(stored) if source and source.base else stored
    return stored
