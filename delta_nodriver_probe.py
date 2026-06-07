"""Delta nodriver feasibility probe (throwaway spike).

Tests whether nodriver (real Chrome via CDP) can clear Delta's Akamai block and pull
live SkyMiles award JSON, via three approaches against one fixed route/date:

  A. navigate + capture the GraphQL XHR off the network (CDP)
  B. harvest cookies, replay the POST via httpx
  C. run the GraphQL fetch() inside the page's JS context

Run locally (residential IP, full mode incl. parser validation):
    cd scraper && python scripts/delta_nodriver_probe.py
Run capture-only (datacenter IP, in GitHub Actions under xvfb):
    xvfb-run -a python delta_nodriver_probe.py --capture-only --env datacenter

NOT wired into the scheduler. NOT vendored to api/. delta.py stays parked; this only
READS its parser (DeltaScraper.normalize) for the validation step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import date
from pathlib import Path

# Fixed, light test inputs (one route/date, reused by all three approaches).
ORIGIN = "BOS"
DEST = "SEA"
TRAVEL_DATE = date(2026, 6, 19)

DELTA_URL = "https://offer-api-prd.delta.com/prd/rm-offer-gql"

# Compact GraphQL query — a valid SUBSET of Delta's real selection set. Akamai gates on
# request fingerprint, not query size, and DeltaScraper.normalize() only reads these fields,
# so the trimmed query is sufficient for both feasibility and parser validation. Kept
# identical to scrapers/delta.py's _GRAPHQL_QUERY.
DELTA_QUERY = (
    "query ($offerSearchCriteria: OfferSearchCriteriaInput!) { "
    "gqlSearchOffers(offerSearchCriteria: $offerSearchCriteria) { "
    "offerResponseId gqlOffersSets { "
    "trips { tripId scheduledDepartureLocalTs scheduledArrivalLocalTs "
    "originAirportCode destinationAirportCode stopCnt "
    "totalTripTime { dayCnt hourCnt minuteCnt } "
    "flightSegment { flightLeg { legId "
    "marketingCarrier { carrierCode carrierNum } "
    "operatingCarrier { carrierCode carrierNum } "
    "aircraft { fleetTypeCode subFleetTypeCode } "
    "duration { dayCnt hourCnt minuteCnt } "
    "layover { destinationAirportCode layoverDuration { dayCnt hourCnt minuteCnt } } } } } "
    "offers { offerId "
    "additionalOfferProperties { fareType dominantSegmentBrandId } "
    "offerItems { retailItems { retailItemMetaData { fareInformation { "
    "fareInformationIndex brandByFlightLegs { brandId cosCode } availableSeatCnt "
    "farePrice { totalFarePrice { milesEquivalentPrice { mileCnt } "
    "currencyEquivalentPrice { roundedCurrencyAmt } } } } } } } } } } }"
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _txn_id() -> str:
    """Client-generated transactionid: uuid + '_' + epoch-millis (matches dotcom)."""
    return f"{uuid.uuid4()}_{int(time.time() * 1000)}"


def build_body(origin: str, dest: str, travel_date: date) -> dict:
    """Build the one-way MILES GraphQL request body (one ADT passenger)."""
    return {
        "variables": {
            "offerSearchCriteria": {
                "productGroups": [{"productCategoryCode": "FLIGHTS"}],
                "offersCriteria": {
                    "resultsPageNum": 1,
                    "resultsPerRequestNum": 20,
                    "preferences": {
                        "refundableOnly": False,
                        "showGlobalRegionalUpgradeCertificate": True,
                        "nonStopOnly": False,
                        "excludeBrandTypes": [],
                    },
                    "pricingCriteria": {"priceableIn": ["MILES"]},
                    "flightRequestCriteria": {
                        "currentTripIndexId": "0",
                        "sortableOptionId": None,
                        "selectedOfferId": "",
                        "searchOriginDestination": [
                            {
                                "departureLocalTs": f"{travel_date:%Y-%m-%d}T00:00:00",
                                "destinations": [{"airportCode": dest.upper()}],
                                "origins": [{"airportCode": origin.upper()}],
                            }
                        ],
                        "sortByBrandId": "MAIN",
                        "additionalCriteriaMap": {"rollOutTag": "GBB"},
                    },
                },
                "customers": [{"passengerTypeCode": "ADT", "passengerId": "1"}],
            }
        },
        "query": DELTA_QUERY,
    }


def httpx_headers(txn_id: str) -> dict[str, str]:
    """Full header set for the httpx replay (Approach B) — includes the forbidden-in-fetch
    headers (origin/referer/user-agent) that a browser would set automatically."""
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "airline": "DL",
        "applicationid": "DC",
        "authorization": "GUEST",
        "channelid": "DCOM",
        "content-type": "application/json",
        "origin": "https://www.delta.com",
        "referer": "https://www.delta.com/",
        "transactionid": txn_id,
        "x-app-route": "search",
        "x-app-type": "dcom-shop",
        "user-agent": _USER_AGENT,
    }


def parse_raw(res: dict) -> dict | None:
    """Parse a result's response text as a JSON object, or None if it isn't a JSON dict.

    json.loads can legitimately return a list/int/str; classify() calls .get() on the
    result, so anything that isn't a dict is treated as unparseable (→ BLOCKED).
    """
    try:
        data = json.loads(res.get("text") or "")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def classify(res: dict) -> str:
    """Classify one approach's result: ERROR | INCONCLUSIVE | BLOCKED | EMPTY | DATA."""
    if res.get("error"):
        return "ERROR"
    if res.get("inconclusive"):
        return "INCONCLUSIVE"
    status = res.get("status")
    if status is not None and status >= 400:
        return "BLOCKED"
    data = parse_raw(res)
    if data is None:
        return "BLOCKED"  # 444 / Akamai "Access Denied" HTML / garbage
    sets = (((data.get("data") or {}).get("gqlSearchOffers") or {}).get("gqlOffersSets"))
    if sets is None:
        return "BLOCKED"
    return "DATA" if sets else "EMPTY"


def validate(data: dict) -> list:
    """Run the captured response through the REAL parked parser → list[FlightRecord].

    Lazily inserts the scraper repo root on sys.path so `scrapers.delta` imports regardless
    of cwd. Only called in full (non --capture-only) mode, so the jobs copy never touches it.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scrapers.delta import DeltaScraper

    return DeltaScraper().normalize(data, ORIGIN, DEST, TRAVEL_DATE)


# --------------------------------------------------------------------------------------
# Browser-driven approaches (nodriver). nodriver is imported lazily so the pure helpers +
# tests don't require it.
# --------------------------------------------------------------------------------------


async def warm_session(browser):
    """Navigate delta.com so Akamai sets _abck / bm_* / ak_bmsc cookies. Returns the tab."""
    tab = await browser.get("https://www.delta.com/")
    await tab.sleep(8)  # let the Akamai sensor POST + cookie set settle
    return tab


async def approach_a_navigate_capture(tab) -> dict:
    """A: passively capture any rm-offer-gql XHR the page fires (best-effort deep link).

    Delta has no stable GET deep link to award results, so this attempts the award-search
    entry and captures whatever GraphQL response the SPA emits. If nothing fires (deep link
    unstable / needs full form interaction), returns INCONCLUSIVE — explicitly allowed by the
    spec rather than sinking the spike into Delta's form automation.
    """
    from nodriver import cdp

    captured_ids: list = []

    def on_response(evt):
        try:
            if "rm-offer-gql" in evt.response.url:
                captured_ids.append(evt.request_id)
        except Exception:
            pass

    tab.add_handler(cdp.network.ResponseReceived, on_response)
    await tab.send(cdp.network.enable())

    deep_link = (
        "https://www.delta.com/flight-search/search-results?"
        f"tripType=ONE_WAY&priceType=MILES&originCity={ORIGIN}&destinationCity={DEST}"
        f"&departureDate={TRAVEL_DATE:%Y-%m-%d}&paxCount=1"
    )
    await tab.get(deep_link)
    await tab.sleep(12)

    for rid in captured_ids:
        try:
            result = await tab.send(cdp.network.get_response_body(rid))
            body = result[0] if isinstance(result, (tuple, list)) else result
            return {"status": 200, "text": body}
        except Exception:
            continue
    return {"inconclusive": True, "text": ""}


async def approach_b_cookie_replay(browser) -> dict:
    """B: harvest the browser cookie jar, replay the POST via httpx (likely fails — _abck is
    fingerprint-bound — which is itself a useful result)."""
    import httpx

    cookies = await browser.cookies.get_all()
    jar = {c.name: c.value for c in cookies if "delta.com" in (getattr(c, "domain", "") or "")}
    body = build_body(ORIGIN, DEST, TRAVEL_DATE)
    headers = httpx_headers(_txn_id())
    with httpx.Client(http2=True, timeout=30.0) as client:
        r = client.post(DELTA_URL, json=body, headers=headers, cookies=jar)
    return {"status": r.status_code, "text": r.text}


async def approach_c_in_page_fetch(tab) -> dict:
    """C: run the GraphQL fetch() inside the page's JS context (carries the browser's cookies
    + fingerprint automatically; origin/referer/user-agent are set by the browser)."""
    body = build_body(ORIGIN, DEST, TRAVEL_DATE)
    js = f"""
    (async () => {{
      const res = await fetch({json.dumps(DELTA_URL)}, {{
        method: 'POST',
        headers: {{
          'accept': 'application/json, text/plain, */*',
          'content-type': 'application/json',
          'airline': 'DL', 'applicationid': 'DC', 'authorization': 'GUEST',
          'channelid': 'DCOM', 'transactionid': {json.dumps(_txn_id())},
          'x-app-route': 'search', 'x-app-type': 'dcom-shop'
        }},
        body: JSON.stringify({json.dumps(body)}),
        credentials: 'include'
      }});
      const text = await res.text();
      return JSON.stringify({{ status: res.status, text: text }});
    }})()
    """
    out = await tab.evaluate(js, await_promise=True)
    payload = json.loads(out if isinstance(out, str) else str(out))
    return {"status": payload.get("status"), "text": payload.get("text") or ""}


async def _launch_connected_browser(headless: bool):
    """Launch Chrome ourselves, wait for its CDP port, then connect nodriver to it.

    Works around nodriver's short self-spawn wait (~0.25s + 5×0.5s ≈ 2.75s in 0.50.3), which
    loses the race with Chrome's cold start when Chrome is spawned via asyncio in some
    environments (measured ~4s here, and likely on CI under xvfb). We spawn Chrome ourselves,
    poll /json/version until it actually serves, then call uc.start(host, port) — passing both
    host and port makes nodriver connect to the *existing* instance instead of spawning (and
    racing) its own. Returns (browser, chrome_process, profile_dir) for teardown.
    """
    import nodriver as uc
    from nodriver.core.config import find_chrome_executable
    from nodriver.core.util import free_port

    port = free_port()
    profile = tempfile.mkdtemp(prefix="delta_probe_")
    # --remote-allow-origins=* is REQUIRED: modern Chrome rejects the CDP websocket upgrade
    # from a non-browser client without it. The rest mirror nodriver's own default flags.
    flags = [
        "--remote-allow-origins=*",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--homepage=about:blank",
        "--no-pings",
        "--password-store=basic",
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-search-engine-choice-screen",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",  # required on CI (root); harmless locally
    ]
    if headless:
        flags.append("--headless=new")
    proc = subprocess.Popen(
        [find_chrome_executable(), *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    version_url = f"http://127.0.0.1:{port}/json/version"
    for _ in range(60):  # up to ~30s for Chrome to open the CDP port
        try:
            urllib.request.urlopen(version_url, timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError(f"Chrome did not open CDP port {port} within 30s")
    browser = await uc.start(host="127.0.0.1", port=port)
    return browser, proc, profile


async def _run(args) -> None:
    captures_dir = Path(__file__).resolve().parent / "captures"
    captures_dir.mkdir(exist_ok=True)

    browser, chrome_proc, profile = await _launch_connected_browser(args.headless)
    rows: list[tuple] = []
    try:
        tab = await warm_session(browser)
        approaches = [
            ("A navigate+capture", lambda: approach_a_navigate_capture(tab)),
            ("B cookie+httpx", lambda: approach_b_cookie_replay(browser)),
            ("C in-page fetch", lambda: approach_c_in_page_fetch(tab)),
        ]
        for name, run in approaches:
            try:
                res = await run()
            except Exception as exc:  # noqa: BLE001 — one approach must not sink the others
                res = {"error": repr(exc), "text": ""}
            label = classify(res)
            slug = name.split()[0]
            fname = f"delta_{slug}_{args.env}_{ORIGIN}-{DEST}_{TRAVEL_DATE}.json"
            (captures_dir / fname).write_text(res.get("text") or json.dumps(res))
            nrec = ""
            if not args.capture_only and label == "DATA":
                data = parse_raw(res)
                if data is not None:
                    nrec = str(len(validate(data)))
            rows.append((name, label, res.get("status"), res.get("error", ""), nrec))
    finally:
        try:
            browser.stop()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            chrome_proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"\n=== Delta nodriver probe — env={args.env} {ORIGIN}->{DEST} {TRAVEL_DATE} ===")
    print(f"{'approach':<20}{'status':<10}{'http':<8}{'#records':<10}error")
    for name, label, status, error, nrec in rows:
        print(f"{name:<20}{label:<10}{str(status):<8}{nrec:<10}{error}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Delta nodriver feasibility probe")
    ap.add_argument(
        "--capture-only",
        action="store_true",
        help="Skip DeltaScraper validation (used for the datacenter/jobs run)",
    )
    ap.add_argument("--env", default="residential", help="Label for output filenames/summary")
    ap.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (default: headful — Akamai scores headless harshly; on CI "
        "run headful under xvfb instead)",
    )
    args = ap.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
