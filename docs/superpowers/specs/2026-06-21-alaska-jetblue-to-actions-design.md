# Alaska + JetBlue award scrapers → GitHub Actions sharded crons — migration design

**Date:** 2026-06-21
**Status:** Design — approved direction, pre-implementation
**Repos:** `jobs/` (new entry points + workflows, vendored scrapers — the bulk), `scraper/` (canonical scraper source; remove the two scheduler jobs at cutover), `api/` (unchanged)
**Related:** the AS/B6→Actions feasibility workflow (2026-06-21, verdict: viable, WAF gate PASSED); the existing award browser scrapers (`delta_browser_scrape.py`, `browser_scrape_common.py`) this mirrors.

## Goal & scope

Move the **scheduled** refresh of Alaska (`AS`) + JetBlue (`B6`) award availability off the
always-on `point-pilot-scraper` Fly box onto **sharded GitHub Actions crons** in the PUBLIC
`jobs/` repo (free minutes, free per-shard Azure IPs), the same way Delta/Southwest/Turkish/
Etihad already run. End state: **3 Fly apps → 2** — retire `point-pilot-scraper`; `point-pilot-api`
and `point-pilot-gflights` stay on Fly.

**The API's on-demand inline AS/B6 scrape is untouched** (`api/main.py` registry +
`api/routers/search.py` `_launch_inline_scrape`). It keeps self-healing any Pro-searched route on
a cache miss regardless of cron cadence — the freshness backstop that makes this safe.

## Why this is viable (gate passed)

- **WAF gate GREEN (the only real blocker):** the 2026-06-21 probe ran ~305 award requests across
  4 distinct Azure IPs (single-IP sustained + 3 parallel) → **0 × 403/406**. Alaska's Fastly
  IP-volume WAF and JetBlue's APIM tolerate GitHub-Actions egress. Probe also validated the exact
  request shapes (AS `__data.json` GET + homepage prime; B6 POST + APIM key).
- **Lightweight:** AS/B6 are pure **httpx** (`HttpScraper`) — a plain `ubuntu-latest` python job,
  **no chromium container / xvfb** (unlike the award browser scrapers). `run_scrape` fits directly
  because AS/B6 are award-shaped `scraper.scrape(o,d,date)` (Delta-shaped), no cash-style loop.
- **Pattern + data layer already exist:** `browser_scrape_common.build_queue_plan` reads the same
  `pp.routes_queue`/`QueueManager` AS/B6 use; `pp_db` (incl. the queries AS/B6 need) is already
  vendored in `jobs/`. New entry points are ~40-line clones of `delta_browser_scrape.py`.
- **Free compute + route-expansion headroom:** sharding across free Azure IPs both retires a box
  and lets the catalogue grow later (more shards = more per-IP WAF-safe volume).

## Non-goals

- **The cash / CPP scraper (`point-pilot-gflights`) — NOT in scope.** It stays on Fly for now;
  revisit after the 06-24 CPP-throughput measurement. (It's browser, backstop-less, and its
  constraint isn't IP diversity — a separate decision.) So this is "retire the AS/B6 box", not
  "Fly only for the API" yet.
- **Route-catalogue expansion** — not part of this migration (replicate current coverage first).
  Sizing a bigger catalogue needs the per-IP ceiling probe (deferred).
- **Decommissioning the Fly box** is the final cutover step, gated on the parallel-run soak.

## Design

### 1. Vendor the scraper source into `jobs/`
Copy `scrapers/alaska.py`, `scrapers/jetblue.py`, `scrapers/base.py` (+ `pipeline/normalizer.py`,
`pipeline/queue_manager.py` if not already present, and the AS/B6 `config` constants) from `scraper/`
(canonical) into `jobs/` — the same managed-subset vendoring already used for `api/`. This adds a
**3rd copy** of `alaska.py`/`jetblue.py`/`base.py`; document the 3-way sync (scraper → api → jobs)
in `scraper/CLAUDE.md` so the "edit-here-propagate-there" footgun is recorded. `pp_db` is already
vendored. No browser deps needed (httpx only).

### 2. Entry points (`jobs/alaska_scrape.py`, `jobs/jetblue_scrape.py`)
~40-line clones of `delta_browser_scrape.py`'s cron path, but instantiating the **httpx** scraper
(`AlaskaScraper()` / `JetBlueScraper()`) instead of the browser scraper:
read `<AIRLINE>_SHARDS` / `<AIRLINE>_SHARD_INDEX` from env → `build_queue_plan(airline=, shard_index=,
shards=, max_legs=)` → `run_scrape(...)` (marks `qm.mark_scraped` per route, per-tier cadence) →
`time.sleep(3); os._exit(0)` (nodriver isn't used, but keep the hard-exit convention harmless / for
any lingering tasks). On-demand single-route mode (origin/dest/dates inputs) only on shard 0, mirroring
the award entry points — optional for AS/B6 since the API already owns on-demand.

### 3. Workflows (`jobs/.github/workflows/{alaska,jetblue}-scrape.yml`)
Plain `runs-on: ubuntu-latest` (NO `container:` — httpx needs no Chrome), `actions/checkout` +
`setup-python` + `pip install` the scraper deps, one `python <airline>_scrape.py` step passing
`DATABASE_URL` (+ optional `BETTERSTACK_SOURCE_TOKEN`, `<AIRLINE>_HEARTBEAT_URL`) and the shard env.
`strategy.matrix.shard` for the fan-out, `fail-fast: false`, `timeout-minutes` generous.

### 4. Guardrail fixes (do BEFORE cutover)
- **Per-tier expiry stamping.** `browser_scrape_common.py` currently flat-stamps `PriorityTier.MED`
  (24h) regardless of a route's adaptive tier, whereas the Fly scheduler stamps per real tier
  (`scheduler.py` `stamp_expiry(valid, job.tier)`). For the wedge airlines this mis-sets HIGH (8h)
  and LOW (48h) windows. Fix `run_scrape` to stamp per-tier — also a latent fix for the existing
  cron airlines.
- **Dense/sparse date window.** The Fly scheduler scrapes a dense-14d + sparse-to-30d window
  (`base.py` `dense_days`/`sparse_step`); `build_queue_plan` emits a flat `range(scrape_days)`.
  Thread the dense/sparse window through so AS/B6's request-volume profile (and WAF exposure) matches
  the proven Fly profile.

## Shard count & cadence (starting points — validate in the soak)

The Fly box already scrapes the **full AS catalogue from a single IP** successfully, so a single
Azure IP is proven-safe at current volume; sharding is for resilience (a block isolates to one
shard) + future headroom. Conservative start, sized to the 24h MED TTL with the existing 20-job
free-plan concurrency ceiling (Southwest 5 + Delta 3 + Turkish 2 = 10 already in use) in view:
- **Alaska:** 2–3 shards, **2–3 runs/day** (the 24h MED TTL needs more than one daily cron;
  free runs make this cheap). Offset cron minutes clear of the 08:00–11:00 UTC award block.
- **JetBlue:** 1–2 shards, **2 runs/day** (B6 is ~13 pairs, small).
Tune both from the parallel-run soak (watch `max(scraped_at_utc)` per route). The per-IP **ceiling
probe** (ramp one Azure IP to find the 406 threshold) is deferred — only needed when *expanding* the
catalogue, not for replicating current coverage.

## Cutover (parallel-run, then decommission)

1. Land the vendoring + entry points + workflows + guardrail fixes (PR to `jobs/`).
2. Run the AS/B6 Actions crons **in parallel** with the Fly scheduler for ~1 week. Watch Better
   Stack `max(scraped_at_utc)` by route + heatmap/best-deals fresh-row coverage; confirm Actions
   keeps AS/B6 inside the 24h MED TTL with margin and that cron skew/skips don't stale cold routes.
3. When Actions coverage matches the Fly box, remove `refresh_alaska_routes` + `refresh_jetblue_routes`
   from `scraper/pipeline/scheduler.py` and **decommission the `point-pilot-scraper` Fly app**
   (`flyctl apps destroy`). API + gflights stay.

## Cross-repo / vendoring

`scrapers/{alaska,jetblue,base}.py` are canonical in `scraper/`, already vendored to `api/`; this
adds a 3rd copy in `jobs/`. Fix in `scraper/` first, propagate to `api/` + `jobs/`. Record the
3-way sync in `scraper/CLAUDE.md`. (Optional: a CI assertion that the vendored copies' hashes match.)

## Observability

The entry points emit the existing per-run `scrape_run` metric + heartbeat (`obs.py`, already in
`jobs/`). Per-shard runs each ship their own metric — widen the Better Stack heartbeat
expected-interval from the ~3h Fly loop to the cron cadence (or it false-alarms), and aggregate
across shards on dashboards. Coverage is observable via `max(scraped_at_utc)` per route.

## Testing

- The **per-tier stamping fix** in `run_scrape` needs a unit test (assert HIGH→8h / MED→24h /
  LOW→48h, not flat MED). It touches the shared award-cron path, so re-run the existing
  Delta/SW/TK/EY hermetic tests.
- Entry points: a smoke test that `build_queue_plan` slices `[shard_index::shards]` for AS/B6.
- CI is hermetic (no live DB / secrets).
- The WAF behaviour is already validated by the probe; the parallel-run soak is the live validation.

## Risks

- **Cron skew vs the 24h MED TTL** for cold/free-tier/browse routes (the on-demand backstop is
  Pro+searched-only). Mitigation: 2–3 free runs/day + the soak gate before decommissioning Fly.
- **20-job concurrency ceiling** shared with the existing award crons — budget AS/B6 shard counts +
  offset cron times clear of the award block.
- **Vendoring drift** (3rd copy) — propagate scraper→api→jobs together; record the sync.
- **Wedge-freshness during the soak** — AS/B6 freshness is the product differentiator; keep the Fly
  box running until Actions coverage demonstrably matches.

## Resolved decisions

| Decision | Choice |
|---|---|
| Scope | AS + B6 scheduled refresh → GA; cash + API stay on Fly |
| Cash scraper | Deferred (revisit post-06-24); not "Fly only for API" yet |
| Runner | Plain `ubuntu-latest` (httpx, no chromium container) |
| Sharding | AS 2–3 shards, B6 1–2; tune in soak; per-IP ceiling probe deferred to route-expansion |
| Cadence | 2–3 runs/day, offset clear of the 08–11 UTC award block (24h MED TTL needs >1/day) |
| Expiry stamping | Fix `run_scrape` to stamp per-tier (also fixes existing cron airlines) |
| Date window | Thread the dense/sparse window through `build_queue_plan` |
| Cutover | Parallel-run ~1 week → soak gate → remove 2 scheduler jobs + destroy the Fly app |
