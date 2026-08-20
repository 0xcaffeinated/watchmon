"""Price history in SQLite.

One row per (product, calendar day) holding the cheapest price seen that day.

Why daily buckets rather than every observation: at ~1,000 tracked products and
a 10-minute schedule, raw observations would add ~144,000 rows a day and the
median would measure how often the monitor ran rather than what the price was.
A daily series makes "median of the last 30 days" mean what it sounds like.
"""

from __future__ import annotations

import sqlite3
import statistics
from contextlib import closing
from pathlib import Path

from . import config
from .models import Listing, PriceStats, day_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    pid           TEXT PRIMARY KEY,
    brand         TEXT,
    title         TEXT,
    url           TEXT,
    first_seen    TEXT,
    last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS daily_prices (
    pid   TEXT NOT NULL,
    day   TEXT NOT NULL,
    price INTEGER NOT NULL,
    PRIMARY KEY (pid, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_pid_day ON daily_prices(pid, day);
"""


class PriceHistory:
    """Append-only price record. Safe to use from one process at a time."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        # WAL so a long read can't block the write at the end of a run.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------ writes ---

    @staticmethod
    def _relative(url: str) -> str:
        """Store the path only, never the origin.

        This database is committed to a public repo so history survives between
        runs, and a full URL would publish the storefront's domain in every
        row. The origin is configuration (config.SITE_BASE) and is added back
        when a link is needed.
        """
        base = config.SITE_BASE
        if base and (url or "").startswith(base):
            return url[len(base):] or "/"
        return url or ""

    def record(self, listings: list[Listing], now: float) -> int:
        """Store today's cheapest price for each priced listing.

        Returns the number of listings recorded. Re-recording the same product
        later the same day keeps the lower price, so an intraday dip survives
        even if the price bounces back before the next run.
        """
        day = day_of(now)
        stamped = [x for x in listings if x.price is not None]
        if not stamped:
            return 0

        with closing(self._connect()) as conn:
            conn.executemany(
                """INSERT INTO products (pid, brand, title, url, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pid) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       title     = COALESCE(NULLIF(excluded.title, ''), products.title),
                       brand     = COALESCE(excluded.brand, products.brand)""",
                [(x.pid, x.brand, x.title, self._relative(x.url), day, day) for x in stamped],
            )
            conn.executemany(
                """INSERT INTO daily_prices (pid, day, price) VALUES (?, ?, ?)
                   ON CONFLICT(pid, day) DO UPDATE SET
                       price = MIN(daily_prices.price, excluded.price)""",
                [(x.pid, day, x.price) for x in stamped],
            )
            conn.commit()
        return len(stamped)

    # ------------------------------------------------------------- reads ---

    def stats(self, pid: str, now: float, window_days: int) -> PriceStats:
        """History for one product, **excluding today**.

        Today is excluded so a price that has already been recorded this run
        can't drag its own median down or count as its own all-time low.
        """
        today = day_of(now)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT day, price FROM daily_prices
                   WHERE pid = ? AND day < ? ORDER BY day DESC""",
                (pid, today),
            ).fetchall()

        if not rows:
            return PriceStats()

        window = [price for _, price in rows[:window_days]]
        return PriceStats(
            days=len(rows),
            median=int(statistics.median(window)),
            min_ever=min(price for _, price in rows),
        )

    def stats_many(self, pids: list[str], now: float, window_days: int) -> dict[str, PriceStats]:
        """stats() for many products in one pass — one query, not N."""
        if not pids:
            return {}
        today = day_of(now)
        placeholders = ",".join("?" * len(pids))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT pid, day, price FROM daily_prices
                    WHERE pid IN ({placeholders}) AND day < ?
                    ORDER BY pid, day DESC""",
                (*pids, today),
            ).fetchall()

        grouped: dict[str, list[int]] = {}
        for pid, _day, price in rows:
            grouped.setdefault(pid, []).append(price)

        out = {}
        for pid in pids:
            prices = grouped.get(pid, [])
            if not prices:
                out[pid] = PriceStats()
                continue
            out[pid] = PriceStats(
                days=len(prices),
                median=int(statistics.median(prices[:window_days])),
                min_ever=min(prices),
            )
        return out

    def product(self, pid: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT pid, brand, title, url, first_seen, last_seen FROM products WHERE pid = ?",
                (pid,),
            ).fetchone()
        if not row:
            return None
        keys = ("pid", "brand", "title", "url", "first_seen", "last_seen")
        record = dict(zip(keys, row))
        if record["url"].startswith("/"):
            record["url"] = config.SITE_BASE + record["url"]
        return record

    def summary(self) -> dict:
        with closing(self._connect()) as conn:
            products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            points = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
            span = conn.execute("SELECT MIN(day), MAX(day) FROM daily_prices").fetchone()
            by_brand = conn.execute(
                "SELECT brand, COUNT(*) FROM products GROUP BY brand ORDER BY COUNT(*) DESC"
            ).fetchall()
        return {
            "products": products,
            "price_points": points,
            "first_day": span[0],
            "last_day": span[1],
            "by_brand": by_brand,
        }

    def series(self, pid: str) -> list[tuple[str, int]]:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT day, price FROM daily_prices WHERE pid = ? ORDER BY day", (pid,)
            ).fetchall()
