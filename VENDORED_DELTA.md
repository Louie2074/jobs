# Vendored Delta scraper (for delta-browser-scrape)

`scrapers/`, `config/`, `db/`, `pipeline/` here let this repo run the nodriver Delta browser
scrape **self-contained** (GH Actions Azure IP clears Akamai where Fly's IP gets HTTP 444), with
no cross-repo checkout / PAT.

Files: `scrapers/{base,browser,delta}.py`, `config/settings.py`, `db/{connection,queries,schema}.py`,
`pipeline/normalizer.py`. Entry point: `delta_browser_scrape.py`.

**`scrapers/browser.py` + `scrapers/delta.py` are CANONICAL HERE** — the Delta browser scraper
was moved out of points-pilot-scrapers into this repo (the scraper repo no longer carries Delta).
The other files (`base.py`, `config/settings.py`, `db/*`, `pipeline/normalizer.py`) are copies of
**points-pilot-scrapers** shared modules — those stay canonical in the scraper repo (Alaska/JetBlue
use them), so re-sync them here if they change upstream. Editing the shared copies without
propagating is the footgun.
