"""Data carried between the scraper, the detectors and the notifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def day_of(ts: float) -> str:
    """Local calendar day for an epoch timestamp — the history bucket key."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


@dataclass
class Rule:
    """One thing worth watching, expressed as data.

    A rule is the *only* place a product category is described. Everything
    else — history, steal detection, scheduling, notification — is category
    agnostic, so adding "mechanical keyboards under 6k with brown switches"
    means appending a Rule, not touching any logic.

    `require_spec` / `reject_spec` map a specifications-table label to a
    pattern its value must (or must not) match. That generalises the original
    hard-coded "Movement must say Automatic, not Hand Winding" check.
    """

    name: str
    brands: tuple[str, ...]
    match_query: str = "{brand}"
    history_query: str = "{brand}"
    include: str | None = None
    ceiling: int | None = None
    require_spec: dict[str, str] = field(default_factory=dict)
    reject_spec: dict[str, str] = field(default_factory=dict)
    mute: tuple[str, ...] = ()

    def queries(self, wide: bool = False) -> list[str]:
        template = self.history_query if wide else self.match_query
        return [template.format(brand=b) for b in self.brands]


@dataclass
class Listing:
    """A product as it appears on a search results card.

    `price` here is advisory: it comes from a text blob and is only used to
    decide what is worth opening and to feed history. The product page decides
    anything that triggers an alert.
    """

    pid: str
    url: str
    title: str
    price: int | None
    brand: str | None = None
    rule: str | None = None


@dataclass
class PriceStats:
    """What history knows about one product, excluding today."""

    days: int = 0
    median: int | None = None
    min_ever: int | None = None

    @property
    def has_enough_history(self) -> bool:
        return self.days > 0 and self.median is not None


@dataclass
class Deal:
    """Something worth telling the user about."""

    pid: str
    url: str
    brand: str
    title: str
    price: int
    kind: str  # "under_threshold" | "steal"
    rule: str = ""
    reason: str = ""
    movement: str = ""
    in_stock: bool = True
    stock_note: str = ""
    stats: PriceStats = field(default_factory=PriceStats)
    seen_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "url": self.url,
            "brand": self.brand,
            "title": self.title,
            "price": self.price,
            "kind": self.kind,
            "reason": self.reason,
            "movement": self.movement,
            "in_stock": self.in_stock,
            "stock_note": self.stock_note,
            "history_days": self.stats.days,
            "median": self.stats.median,
            "min_ever": self.stats.min_ever,
            "seen_at": self.seen_at,
        }

    @property
    def alert_key(self) -> str:
        """Dedup key. Kind is included so a watch can raise both alert types."""
        return f"{self.kind}:{self.pid}"
