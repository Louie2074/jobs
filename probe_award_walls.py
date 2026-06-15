"""Datacenter-IP bot-wall probe for candidate award-search airlines.

Throwaway recon (NOT a scraper): mirrors the BrowserScraper transport (spawn headful Chrome,
connect nodriver via CDP, warm-navigate, dwell, in-page fetch) to answer ONE question per
airline from the GitHub Actions (Azure) IP: does a real browser reach the award-search flow
clean, or does the bot wall escalate to a CAPTCHA / Access-Denied / queue interstitial (the
thing that makes American hard)?

Run via the `probe-award-walls` workflow (workflow_dispatch) under xvfb. Writes nothing to the
DB; prints a per-airline JSON summary and uploads screenshots as artifacts.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import time
import urllib.request

# (name, warm_url, probe_url) — warm seeds the anti-bot sensor on the site, probe is the award
# entry point we actually evaluate. Same value => single navigation.
TARGETS = [
    ("air_france",     "https://www.airfrance.us/",                                                         "https://www.airfrance.us/"),
    ("turkish",        "https://www.turkishairlines.com/en-us/",                                            "https://www.turkishairlines.com/en-us/miles-and-smiles/"),
    ("avianca",        "https://www.lifemiles.com/",                                                        "https://www.lifemiles.com/fly/find"),
    ("cathay",         "https://www.cathaypacific.com/cx/en_US/",                                           "https://www.cathaypacific.com/cx/en_US/book-a-trip/redeem-flights/redeem-flight-awards.html"),
    ("qantas",         "https://www.qantas.com/us/en/",                                                     "https://www.qantas.com/us/en/book-a-trip/flights.html"),
    ("etihad",         "https://digital.etihad.com/",                                                       "https://digital.etihad.com/book/search?FLOW=AWARD&B_LOCATION=JFK&E_LOCATION=AUH&TRIP_TYPE=O&CABIN=E&TRAVELERS=ADT&DATE_1=202607080000"),
    ("virgin_atlantic","https://www.virginatlantic.com/",                                                   "https://www.virginatlantic.com/"),
    ("tap",            "https://www.flytap.com/en-us/",                                                     "https://booking.flytap.com/"),
]

# substrings (lowercased) that mean the wall escalated. captcha/queue called out separately.
CAPTCHA = ["recaptcha", "hcaptcha", "g-recaptcha", "px-captcha", "funcaptcha", "are you a robot",
           "verify you are human", "verify you're human", "i'm not a robot", "unusual traffic",
           "challenges.cloudflare.com", "just a moment", "attention required", "checking your browser"]
QUEUE = ["queue-it", "you are now in line", "waiting room", "your estimated wait"]
DENIED = ["access denied", "you don't have permission", "request unsuccessful. incapsula",
          "_incapsula_resource", "access to this page has been denied", "reference #18."]


def classify(status, body_l):
    if any(m in body_l for m in CAPTCHA):
        return "CAPTCHA"
    if any(m in body_l for m in QUEUE):
        return "QUEUE"
    if any(m in body_l for m in DENIED):
        return "DENIED"
    if isinstance(status, int) and status in (403, 406, 429, 444):
        return f"BLOCKED_{status}"
    return "LOADED"


async def main():
    import nodriver as uc
    from nodriver.core.config import find_chrome_executable
    from nodriver.core.util import free_port

    port = free_port()
    profile = tempfile.mkdtemp(prefix="probe_")
    flags = [
        "--remote-allow-origins=*", "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
        "--homepage=about:blank", "--no-pings", "--password-store=basic",
        "--disable-breakpad", "--disable-dev-shm-usage", "--disable-infobars",
        "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
        "--disable-features=IsolateOrigins,site-per-process", "--no-sandbox",
    ]
    proc = subprocess.Popen([find_chrome_executable(), *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.5)
    browser = await uc.start(host="127.0.0.1", port=port)

    results = []
    for name, warm, probe in TARGETS:
        rec = {"airline": name, "probe_url": probe}
        try:
            tab = await browser.get(warm)
            await tab.sleep(10)  # let the anti-bot sensor run + settle
            if probe != warm:
                await tab.get(probe)
                await tab.sleep(10)
            # rendered DOM state
            title = await tab.evaluate("document.title") or ""
            href = await tab.evaluate("location.href") or ""
            body = await tab.evaluate("document.body ? document.body.innerText.slice(0,6000) : ''") or ""
            body_l = str(body).lower()
            # in-page fetch of the probe URL (same-origin) → status the warmed session sees
            fjs = ("(async()=>{try{const r=await fetch(" + json.dumps(probe) +
                   ",{credentials:'include'});const t=await r.text();"
                   "return JSON.stringify({s:r.status,n:t.length,h:t.slice(0,300)});}"
                   "catch(e){return JSON.stringify({s:'ERR',n:0,h:String(e).slice(0,120)});}})()")
            fetched = await tab.evaluate(fjs, await_promise=True)
            try:
                f = json.loads(fetched) if isinstance(fetched, str) else {"s": "?", "n": 0, "h": ""}
            except Exception:
                f = {"s": "?", "n": 0, "h": ""}
            fbody_l = str(f.get("h", "")).lower()
            verdict_nav = classify(None, body_l)
            verdict_fetch = classify(f.get("s"), fbody_l)
            # combined: any block signal wins
            order = {"CAPTCHA": 4, "DENIED": 3, "QUEUE": 2}
            combined = max([verdict_nav, verdict_fetch], key=lambda v: order.get(v.split("_")[0], 1 if v.startswith("BLOCKED") else 0))
            rec.update({
                "final_url": str(href)[:90], "title": str(title)[:70],
                "nav_verdict": verdict_nav, "fetch_status": f.get("s"),
                "fetch_len": f.get("n"), "fetch_verdict": verdict_fetch,
                "VERDICT": combined,
            })
            try:
                await tab.save_screenshot(f"shot_{name}.png")
            except Exception as e:
                rec["shot_err"] = str(e)[:60]
        except Exception as e:
            rec.update({"VERDICT": "PROBE_ERR", "error": f"{type(e).__name__}: {str(e)[:120]}"})
        print("RESULT " + json.dumps(rec), flush=True)
        results.append(rec)

    print("\n===== SUMMARY =====", flush=True)
    for r in results:
        print(f"{r['airline']:18} {r.get('VERDICT','?'):12} "
              f"nav={r.get('nav_verdict','-')} fetch={r.get('fetch_status','-')}/{r.get('fetch_verdict','-')} "
              f"len={r.get('fetch_len','-')}", flush=True)
    try:
        browser.stop()
    except Exception:
        pass
    proc.terminate()


if __name__ == "__main__":
    uc = __import__("nodriver")
    uc.loop().run_until_complete(main())
