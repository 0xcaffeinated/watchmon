# watchmon

Watches a set of brands on an online storefront and alerts on two independent
rules:

1. **Automatic under a price ceiling** — any automatic-movement watch below it.
2. **Steal** — any watch that has fallen far below *its own* recorded price
   history.

Alerts go to a phone via [ntfy](https://ntfy.sh) and, on macOS, a native banner.

The storefront is **not hardcoded**. It comes from `WATCH_SITE_BASE` (or a
gitignored `site.txt` locally), so this repository names no retailer.

## Configure

| Setting | Where | Notes |
|---|---|---|
| `WATCH_SITE_BASE` | env / `site.txt` | origin only, e.g. `https://shop.example.com` |
| `NTFY_TOPIC` | env / `ntfy_topic.txt` | the topic name is the only access control — keep it unguessable |

Everything else lives in `watchmon/config.py`: brands, price ceiling, steal
thresholds, muted model families, cadence.

## Run

```bash
python monitor.py                 # one guarded check
python monitor.py --status        # run state + history summary + live rules
python monitor.py --history <pid> # price series for one product
python monitor.py --dry-run       # scrape + print JSON, no alerts, no state
python monitor.py --report        # 24h health report
uv run --with pytest python -m pytest tests/ -q   # 168 tests, no network
```

Locally on macOS, `./install.sh` schedules it with launchd. In CI,
`.github/workflows/check.yml` runs it on a cron.

## Layout

```
monitor.py         CLI only — argument parsing and output
watchmon/
  config.py        every tunable and path
  models.py        Listing, Deal, PriceStats
  parsing.py       pure text/HTML rules — no I/O, no clock
  history.py       SQLite daily price series
  steals.py        the steal rule (pure)
  scraper.py       the only module that drives a browser
  notify.py        notification channels behind one Notifier
  runstate.py      throttle, backoff, network probe, lock
  report.py        daily health report
  runner.py        Monitor — orchestrates one check
tests/             168 tests, no network required
```

## The two rules

**Price ceiling.** Narrow sweep per brand, every tick. Candidates are confirmed
on the product page before alerting: price from JSON-LD, movement from the
specifications table (which must say automatic or self-winding, not hand-wound).

**Steal.** Wide sweep records the day's cheapest price for every watch of the
watched brands. A steal must clear **both** bars — at least `STEAL_DISCOUNT`
below its 30-day median, **and** at or within 2% of its lowest price ever
recorded — and only after `STEAL_MIN_HISTORY_DAYS` of history, above
`STEAL_MIN_PRICE`.

Both bars are required. Discount alone fires on a median skewed by a price
spike; all-time-low alone fires on a trivial dip. The history minimum is the
load-bearing guard: without it every newly listed product is its own all-time
low and alerts immediately.

## Hard-won details

Every one of these was a silent failure first:

- **Never take the price from the rendered page.** Recommendation carousels
  reuse byte-identical CSS classes, so "first currency symbol on the page"
  reported ₹2,699 for an ₹11,699 watch. JSON-LD only.
- **A cold session cannot search.** Requesting `/search` without cookies hangs
  until timeout from a datacenter IP; one homepage visit first (200, ~11
  cookies) makes the identical request succeed. `StoreScraper` always warms up.
- **`price_asc` is not monotonic across pages** — page 3 ran to ₹58,220 while
  page 4 restarted at ₹6,500 — so paging never stops early on price.
- **An empty page is not the end of results.** The last page is often served
  empty with a nonsense range (`Showing 81 – 80 of 102`); that is read from the
  `Page N of M` footer and treated as completion. A genuinely empty mid-sweep
  page is retried once, then warned about.
- **"Automatic" is not the only word for an automatic.** One brand's automatics
  are listed as "Mechanical" and never "Automatic" — an automatic-only filter
  made the whole brand invisible. The card filter accepts both; the spec table
  decides, and rejects hand-wound.
- **Model codes hide in the `h1`** — not the JSON-LD name, not the URL slug. Mute
  matching therefore runs against the product page, not the search card.
- **Movement comes from a standalone spec row**, never any line containing the
  word: that matched a customer review and made the quartz filter meaningless.
- **History excludes today**, so a price recorded this run cannot become its own
  all-time low or drag its own median down.
- **Stored URLs are paths, not origins** — the database is committed so history
  survives CI runs, and full URLs would publish the storefront.

## Notification behaviour

New qualifying item alerts; a further drop alerts again; the same or a slightly
higher qualifying price is silent. Something that stops qualifying is re-armed
and can alert afresh. The two rules de-duplicate separately, so one product can
raise both.

Out-of-stock listings are dropped rather than alerted
(`ALERT_ON_OUT_OF_STOCK`). That knowingly accepts a risk — the stock signals
disagree, so a false "out of stock" costs a real deal — so every drop is logged
at WARNING as `DROPPED` and counted in the daily report.

## Known gaps

- Discovery depends on the site's search returning a product, and that result
  set is unstable between runs. An item can vanish from results and be treated
  as gone while still live; on a narrow tick only part of the catalogue is
  screened, so steals can re-arm and re-alert.
- CI cron drifts under load, and scheduled workflows auto-disable after 60 days
  without repository activity.
