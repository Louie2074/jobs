"""Staged Turkish transport probe — isolates WHERE the nodriver flow hangs on the Azure IP.

Prints at every stage (chrome launch -> CDP port -> uc.start -> navigate -> readyState -> one
in-page availability fetch). Run with `python -u` so output is unbuffered. A hard os._exit
watchdog guarantees the step ends (and logs flush) instead of riding the job timeout.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request


def P(msg):
    sys.stdout.write(f">>> {msg}\n")
    sys.stdout.flush()


def _watchdog():
    time.sleep(220)
    sys.stdout.write(">>> WATCHDOG 150s — hung, exiting\n")
    sys.stdout.flush()
    os._exit(3)


threading.Thread(target=_watchdog, daemon=True).start()
P("script start")

import nodriver as uc  # noqa: E402
from nodriver.core.config import find_chrome_executable  # noqa: E402
from nodriver.core.util import free_port  # noqa: E402

AVAIL = "https://www.turkishairlines.com/api/v1/availability"
WARM = "https://www.turkishairlines.com/en-us/"


async def main():
    port = free_port()
    prof = tempfile.mkdtemp(prefix="tkprobe_")
    flags = [
        "--remote-allow-origins=*", "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
        "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
        "--homepage=about:blank", "--no-pings", "--password-store=basic",
        "--disable-breakpad", "--disable-dev-shm-usage", "--disable-infobars",
        "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
        "--disable-features=IsolateOrigins,site-per-process", "--no-sandbox",
    ]
    P(f"chrome exe = {find_chrome_executable()}")
    P("launching chrome")
    proc = subprocess.Popen([find_chrome_executable(), *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    P("waiting for CDP port")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            break
        except Exception:
            await asyncio.sleep(0.5)
    P("CDP port up; uc.start")
    browser = await uc.start(host="127.0.0.1", port=port)
    warm_page = "https://www.turkishairlines.com/en-us/miles-and-smiles/book-award-tickets/"
    P(f"navigating to warm page: {warm_page}")
    tab = await browser.get(warm_page)
    P("navigate returned; sleeping 20s (let PerimeterX fully init)")
    await tab.sleep(20)
    rs = await tab.evaluate("document.readyState")
    P(f"readyState = {rs!r}")

    body = {
        "selectedBookerSearch": "O", "selectedCabinClass": "ECONOMY", "moduleType": "AWARD",
        "passengerTypeList": [{"quantity": 1, "code": "ADULT"}],
        "originDestinationInformationList": [
            {"originAirportCode": "SEA", "destinationAirportCode": "IST", "departureDate": "10-07-2026"}],
        "savedDate": "2026-06-16T02:00:00.000Z",
    }
    headers = {
        "accept": "application/json", "content-type": "application/json", "accept-language": "en",
        "x-clientid": "probe-client", "x-requestid": "probe-req", "x-country": "us",
        "x-platform": "WEB", "x-conversationid": "probe-conv",
    }
    js = (
        "(async () => {"
        f"  const res = await fetch({json.dumps(AVAIL)}, {{ method:'POST',"
        f"    headers: {json.dumps(headers)}, body: JSON.stringify({json.dumps(body)}),"
        "     credentials:'include' });"
        "  const t = await res.text();"
        "  return JSON.stringify({ status: res.status, len: t.length, head: t.slice(0,70) });"
        "})()"
    )
    # Fire the fetch several times with gaps — does PerimeterX auto-solve the crypto challenge
    # after the first 428 (then later calls pass), or stay challenged?
    for i in range(5):
        try:
            out = await tab.evaluate(js, await_promise=True)
        except Exception as exc:  # noqa: BLE001
            out = f"EVALERR {type(exc).__name__}: {str(exc)[:60]}"
        P(f"FETCH[{i}]: {out}")
        ck = await tab.evaluate(
            "document.cookie.split(';').map(c=>c.trim().split('=')[0]).filter(k=>/px|_pxhd|sec/i.test(k)).join(',')"
        )
        P(f"  px-cookies: {ck!r}")
        await tab.sleep(8)
    P("DONE — tearing down")
    try:
        browser.stop()
    except Exception:
        pass
    proc.terminate()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
    P("main complete")
