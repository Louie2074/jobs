# Scraper Density And Timezone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the EZE timezone warning and make a small, safe scraper-density increase in GitHub Actions without touching blocked Southwest or raising cash load.

**Architecture:** Keep all behavior in existing config/workflow files. Add timezone coverage to the vendored maps, guard it with a focused test, increase only clean Actions-side award density, and clarify that cash scraping is now Actions-primary.

**Tech Stack:** Python 3.11, pytest, GitHub Actions YAML, vendored scraper modules.

## Global Constraints

- Work only inside `/Users/louisn/Documents/indiehax/point_pilot/.worktrees/scraper-density-jobs`.
- Do not change Southwest density.
- Do not increase `CASH_TOP_ROUTES`, `CASH_SHARDS`, `CASH_SCRAPE_DAYS`, or cash workflow frequency.
- Add `EZE` with exact timezone `America/Argentina/Buenos_Aires`.
- Keep comments/docs consistent with Google Flights cash being Actions-primary and Fly legacy/bake-in.
- Use TDD and commit completed work.

---

### Task 1: Airport Timezone Coverage

**Files:**
- Modify: `config/airport_tz.py`
- Modify: `pp_db/airport_tz.py`
- Create or modify: `tests/test_airport_tz.py`

**Interfaces:**
- Consumes: `AIRPORT_TZ: dict[str, str]`
- Produces: both maps contain `AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_airport_tz.py` if it does not exist:

```python
from config.airport_tz import AIRPORT_TZ as CONFIG_AIRPORT_TZ
from pp_db.airport_tz import AIRPORT_TZ as CASH_AIRPORT_TZ


def test_eze_timezone_is_available_for_jetblue_and_cash_matching():
    assert CONFIG_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
    assert CASH_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_airport_tz.py -q`

Expected before implementation: failure with `KeyError: 'EZE'`.

- [ ] **Step 3: Add EZE to both maps**

Add this exact entry in alphabetical order in both maps:

```python
"EZE": "America/Argentina/Buenos_Aires",  # Buenos Aires Ezeiza — JetBlue route timezone
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_airport_tz.py -q`

Expected: pass.

### Task 2: Safe Actions Density Increase

**Files:**
- Modify: `.github/workflows/jetblue-scrape.yml`
- Modify: `.github/workflows/turkish-browser-scrape.yml`
- Modify: `.github/workflows/etihad-browser-scrape.yml`
- Modify: `config/settings.py`

**Interfaces:**
- Produces: JetBlue workflow matrix uses four shards and `JETBLUE_SHARDS` is `"4"`.
- Produces: Turkish workflow uses `TURKISH_SCRAPE_DAYS: "5"`.
- Produces: Etihad workflow uses `ETIHAD_SCRAPE_DAYS: "5"`.
- Produces: `CRON_MAX_LEGS_PER_SHARD["jetblue"]` remains conservative and comments match four shards.

- [ ] **Step 1: Update JetBlue workflow**

Change the JetBlue strategy matrix from:

```yaml
matrix:
  shard: [0, 1, 2]
```

to:

```yaml
matrix:
  shard: [0, 1, 2, 3]
```

Change:

```yaml
JETBLUE_SHARDS: "3"
```

to:

```yaml
JETBLUE_SHARDS: "4"
```

Update nearby comments to say this is a measured +33% Actions-side density bump after clean metrics.

- [ ] **Step 2: Update Turkish and Etihad windows**

Change:

```yaml
TURKISH_SCRAPE_DAYS: "3"
ETIHAD_SCRAPE_DAYS: "3"
```

to:

```yaml
TURKISH_SCRAPE_DAYS: "5"
ETIHAD_SCRAPE_DAYS: "5"
```

Update comments to call this a near-term 5-day window.

- [ ] **Step 3: Update settings comments**

In `config/settings.py`, update the JetBlue cap comment so it references four shards and the current total candidate pool. Do not raise the per-shard cap.

- [ ] **Step 4: Validate YAML/config text**

Run:

```bash
python - <<'PY'
from pathlib import Path
assert 'shard: [0, 1, 2, 3]' in Path('.github/workflows/jetblue-scrape.yml').read_text()
assert 'JETBLUE_SHARDS: "4"' in Path('.github/workflows/jetblue-scrape.yml').read_text()
assert 'TURKISH_SCRAPE_DAYS: "5"' in Path('.github/workflows/turkish-browser-scrape.yml').read_text()
assert 'ETIHAD_SCRAPE_DAYS: "5"' in Path('.github/workflows/etihad-browser-scrape.yml').read_text()
PY
```

Expected: command exits 0.

### Task 3: Google Flights Migration Clarity

**Files:**
- Modify: `cash_browser_scrape.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: docs state `cash-browser-scrape.yml` is the primary scheduled Google Flights path.
- Produces: docs state Fly gflights is legacy/bake-in and should be stopped or kept scaled down once Actions is verified.

- [ ] **Step 1: Update cash runner docstring**

In `cash_browser_scrape.py`, replace the sentence saying to leave Fly untouched for bake-in with a current note:

```python
The GitHub Actions workflow is now the primary scheduled Google Flights cash path. The old
``google_flights_main.py`` Fly runner is a legacy/bake-in path only; keep it stopped or scaled
down once Actions coverage is verified so it does not double-scrape or emit confusing metrics.
```

- [ ] **Step 2: Update repo docs**

In `CLAUDE.md`, make the Google Flights/cash section point to `.github/workflows/cash-browser-scrape.yml` as primary and call `point-pilot-gflights` legacy/bake-in.

- [ ] **Step 3: Run checks**

Run:

```bash
pytest tests/test_airport_tz.py -q
python -m compileall config pp_db pipeline scrapers -q
```

Expected: both commands pass.

- [ ] **Step 4: Commit**

Run:

```bash
git status --short
git add .github/workflows/jetblue-scrape.yml .github/workflows/turkish-browser-scrape.yml .github/workflows/etihad-browser-scrape.yml config/airport_tz.py pp_db/airport_tz.py tests/test_airport_tz.py config/settings.py cash_browser_scrape.py CLAUDE.md docs/superpowers/specs/2026-06-30-scraper-density-timezone-design.md docs/superpowers/plans/2026-06-30-scraper-density-timezone.md
git commit -m "chore: increase safe scraper density"
```

Expected: commit succeeds.
