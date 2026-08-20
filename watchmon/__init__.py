"""The site watch monitor.

Two independent alert rules over Invicta / Casio / Timex / Alba:

  * any *automatic* under ₹8,000;
  * any watch at a steal price relative to its own recorded history.

Layout:
    config     tunables, paths
    models     dataclasses passed between layers
    parsing    pure text/HTML rules (no I/O)
    history    SQLite daily price series
    steals     the steal rule (pure)
    scraper    the only module that drives a browser
    notify     alert channels
    runstate   throttle, backoff, network, lock
    runner     orchestration
"""

__all__ = ["config", "models", "parsing", "history", "steals", "scraper", "notify", "runstate", "runner"]
