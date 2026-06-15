"""Datacenter-IP award-extraction validation harness (throwaway recon, NOT a scraper).

For each candidate no-login airline, on the GitHub Actions (Azure) IP: launch headful Chrome
(nodriver), warm the award-search page, inject a fetch()/XHR interceptor, best-effort drive the
award search, and record the full request flow. Then classify per airline:
  - did any response carry real AWARD DATA (miles/points/Avios + a number)?  -> end-to-end works
  - was there a SESSION-MINT call (session/cart/conversation/init id) before availability?
This settles stateless-vs-session and proves whether the wall clears end-to-end on the Azure IP.

Writes nothing to the DB. Prints a per-airline summary, dumps full captures + screenshots as
artifacts. Form-driving is best-effort; the captured network flow is the real deliverable.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import urllib.request

# fetch()+XHR interceptor — logs every request's url/method/status/len + a body snippet to
# window.__cap. Injected after warm-nav (before we submit the search), so the award/session
# XHRs are captured. Kept defensive: never throws into page code.
INTERCEPT = r"""
(()=>{ if(window.__cap)return 'already'; window.__cap=[];
  const push=o=>{try{if(window.__cap.length<400)window.__cap.push(o);}catch(e){}};
  const of=window.fetch;
  if(of) window.fetch=function(){const a=arguments; let url='',m='GET';
    try{url=(a[0]&&a[0].url)?a[0].url:(''+a[0]); m=(a[1]&&a[1].method)||(a[0]&&a[0].method)||'GET';}catch(e){}
    return of.apply(this,a).then(r=>{try{r.clone().text().then(t=>push({k:'f',u:String(url).slice(0,200),m,s:r.status,n:(t||'').length,b:(t||'').slice(0,600)})).catch(()=>{});}catch(e){} return r;});};
  const oo=XMLHttpRequest.prototype.open, os=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(m,u){this.__m=m;this.__u=u;return oo.apply(this,arguments);};
  XMLHttpRequest.prototype.send=function(){const x=this; x.addEventListener('load',()=>{try{push({k:'x',u:String(x.__u).slice(0,200),m:x.__m,s:x.status,n:(x.responseText||'').length,b:(x.responseText||'').slice(0,600)});}catch(e){}}); return os.apply(this,arguments);};
  return 'installed';
})()
"""

FUTURE_DAY = "20"   # June 2026 day to click in calendars (run date ~ mid-June)


async def click_text(tab, *texts):
    """Click the first visible element whose text matches any of `texts` (case-insensitive)."""
    js = (
        "(()=>{const ts=" + json.dumps([t.lower() for t in texts]) + ";"
        "const els=[...document.querySelectorAll('button,a,span,div,label,li,[role=button],[role=option],input')];"
        "for(const t of ts){const e=els.find(x=>x.offsetParent&&((x.textContent||'')+(x.value||'')+(x.getAttribute&&x.getAttribute('aria-label')||'')).toLowerCase().trim().includes(t)&&((x.textContent||'').length<80));"
        "if(e){e.scrollIntoView({block:'center'});e.click();return (e.textContent||e.value||'').trim().slice(0,40)||'clicked';}}return null;})()"
    )
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


async def type_airport(tab, city, code):
    """Set `city` into the currently-focused airport input using the native value setter +
    a rich event burst (input/change/keydown/keyup) — React-friendly best-effort. Relies on
    the caller having focused the field via click_text."""
    last = city[-1]
    js = (
        "(()=>{const a=document.activeElement;"
        "if(!a||!('value' in a))return 'noinput';"
        "const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
        "if(set&&set.set)set.set.call(a," + json.dumps(city) + ");else a.value=" + json.dumps(city) + ";"
        "a.dispatchEvent(new Event('input',{bubbles:true}));"
        "a.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:" + json.dumps(last) + "}));"
        "a.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:" + json.dumps(last) + "}));"
        "a.dispatchEvent(new Event('change',{bubbles:true}));"
        "return 'typed:'+a.value;})()"
    )
    try:
        return await tab.evaluate(js)
    except Exception:
        return None


def classify(cap):
    """Return (award_hit, session_hit, award_samples, session_samples)."""
    award, session = [], []
    award_re = ("miles", "avios", '"points"', "milesamount", "awardprice", "milevalue",
                "redemption", "fareawards", "pointsprice")
    sess_re = ("session", "/cart", "conversation", "shoppingid", "basketid", "/init",
               "createsession", "cid=", "correlationid", "offerid", "shoppingcart")
    for r in cap:
        u = (r.get("u") or "").lower()
        b = (r.get("b") or "").lower()
        s = r.get("s")
        n = r.get("n") or 0
        if s == 200 and n > 400 and any(k in b for k in award_re) and any(c.isdigit() for c in b):
            award.append({"u": r.get("u"), "s": s, "n": n})
        if any(k in u for k in sess_re) or any(k in b[:300] for k in ("sessionid", "shoppingid", "conversationid", "basketid", "cartid")):
            session.append({"u": r.get("u"), "m": r.get("m"), "s": s})
    return award[:6], session[:6]


# ----------------------------------------------------------------- per-airline drivers
async def drv_turkish(tab):
    await click_text(tab, "only necessary", "accept only")
    await tab.sleep(1)
    await click_text(tab, "award ticket")           # switch form to award mode
    await tab.sleep(2)
    await click_text(tab, "to")                      # focus destination
    await tab.sleep(1)
    await type_airport(tab, "Istanbul", "IST")
    await tab.sleep(2)
    await click_text(tab, "istanbul airport (ist)", "(ist)")
    await tab.sleep(1)
    await click_text(tab, "dates", "select date", "departure")
    await tab.sleep(1)
    await click_text(tab, FUTURE_DAY)
    await tab.sleep(1)
    await click_text(tab, "ok", "done", "search flights", "search")
    await tab.sleep(10)


async def drv_etihad(tab):
    await click_text(tab, "close", "accept")
    await tab.sleep(1)
    await click_text(tab, "flying to", "select your destination")
    await tab.sleep(1)
    await type_airport(tab, "Abu Dhabi", "AUH")
    await tab.sleep(2)
    await click_text(tab, "abu dhabi", "(auh)")
    await tab.sleep(1)
    await click_text(tab, "continue")
    await tab.sleep(1)
    await click_text(tab, "continue")
    await tab.sleep(1)
    await click_text(tab, "one way")
    await tab.sleep(1)
    await click_text(tab, FUTURE_DAY)
    await tab.sleep(1)
    await click_text(tab, "continue", "search")
    await tab.sleep(12)


async def drv_generic(tab):
    """Best-effort: toggle award/miles mode, fill From/To, pick a date, submit."""
    await click_text(tab, "only necessary", "accept", "reject all", "close", "ok")
    await tab.sleep(1)
    await click_text(tab, "redeem", "pay with miles", "book with miles", "use points",
                     "use miles", "reward seat", "redeem miles", "award")
    await tab.sleep(1)
    # origin
    await click_text(tab, "from", "origin", "flying from", "departure")
    await tab.sleep(1)
    await type_airport(tab, "New York", "JFK")
    await tab.sleep(2)
    await click_text(tab, "(jfk)", "kennedy", "new york")
    await tab.sleep(1)
    # destination
    await click_text(tab, "to", "destination", "flying to", "arrival")
    await tab.sleep(1)
    await type_airport(tab, "London", "LHR")
    await tab.sleep(2)
    await click_text(tab, "(lhr)", "heathrow", "london")
    await tab.sleep(1)
    await click_text(tab, "dates", "select date", "when", "departure")
    await tab.sleep(1)
    await click_text(tab, FUTURE_DAY)
    await tab.sleep(1)
    await click_text(tab, "search", "find flight", "continue", "search flights")
    await tab.sleep(12)


DRIVERS = {
    "turkish":         ("https://www.turkishairlines.com/en-us/", drv_turkish),
    "etihad":          ("https://www.etihad.com/en-us/etihadguest/spend-miles/fly-with-miles", drv_etihad),
    "air_france":      ("https://www.airfrance.us/", drv_generic),
    "avianca":         ("https://www.lifemiles.com/fly/find", drv_generic),
    "cathay":          ("https://www.cathaypacific.com/cx/en_US/book-a-trip/redeem-flights/redeem-flight-awards.html", drv_generic),
    "qantas":          ("https://www.qantas.com/us/en/book-a-trip/flights.html", drv_generic),
    "virgin_atlantic": ("https://www.virginatlantic.com/", drv_generic),
    "tap":             ("https://www.flytap.com/en-us/", drv_generic),
}


async def main():
    import nodriver as uc
    from nodriver.core.config import find_chrome_executable
    from nodriver.core.util import free_port

    port = free_port()
    profile = tempfile.mkdtemp(prefix="testscrape_")
    flags = ["--remote-allow-origins=*", "--remote-debugging-host=127.0.0.1",
             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
             "--homepage=about:blank", "--no-pings", "--password-store=basic",
             "--disable-breakpad", "--disable-dev-shm-usage", "--disable-infobars",
             "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
             "--disable-features=IsolateOrigins,site-per-process", "--no-sandbox"]
    proc = subprocess.Popen([find_chrome_executable(), *flags],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read(); break
        except Exception:
            await asyncio.sleep(0.5)
    browser = await uc.start(host="127.0.0.1", port=port)

    summary = []
    for name, (url, driver) in DRIVERS.items():
        rec = {"airline": name}
        try:
            # blank tab first, install the interceptor on EVERY new document (survives the
            # search's navigation to a results page), then navigate to the award search.
            tab = await browser.get("about:blank")
            try:
                await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(INTERCEPT))
            except Exception as e:
                rec["inject_err"] = str(e)[:60]
            await tab.get(url)
            await tab.sleep(9)
            try:
                await tab.evaluate(INTERCEPT)  # belt-and-suspenders for the first document
            except Exception:
                pass
            try:
                await driver(tab)
            except Exception as e:
                rec["drive_err"] = f"{type(e).__name__}: {str(e)[:80]}"
            try:
                raw = await tab.evaluate("JSON.stringify(window.__cap||[])")
                cap = json.loads(raw) if isinstance(raw, str) else []
            except Exception as e:
                cap = []; rec["cap_err"] = str(e)[:60]
            award, session = classify(cap)
            rec.update({
                "final_url": (await tab.evaluate("location.href") or "")[:90],
                "xhr_count": len(cap),
                "award_data": bool(award), "award_calls": award,
                "session_calls": session,
            })
            try:
                with open(f"cap_{name}.json", "w") as f:
                    json.dump(cap, f)
                await tab.save_screenshot(f"scrape_{name}.png")
            except Exception:
                pass
        except Exception as e:
            rec["fatal"] = f"{type(e).__name__}: {str(e)[:100]}"
        print("RESULT " + json.dumps(rec), flush=True)
        summary.append(rec)

    print("\n===== SUMMARY =====", flush=True)
    for r in summary:
        print(f"{r['airline']:18} award_data={str(r.get('award_data')):5} "
              f"xhr={r.get('xhr_count','-'):>4} session_calls={len(r.get('session_calls',[]))} "
              f"{('ERR '+r.get('drive_err','')) if r.get('drive_err') else ''}", flush=True)
    try:
        browser.stop()
    except Exception:
        pass
    proc.terminate()


if __name__ == "__main__":
    __import__("nodriver").loop().run_until_complete(main())
