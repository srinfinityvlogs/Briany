"""Step 0 hosting spike: can a Vercel function reach NSE, Yahoo and Groq?
Throwaway. Open: https://<your-app>.vercel.app/spike?token=<SPIKE_TOKEN>
"""
import hmac
import os
import time
from collections import Counter

import requests
from fastapi import FastAPI, HTTPException, Query

app = FastAPI()

UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
NSE_HOME = "https://www.nseindia.com"


def timed(name, fn):
    t0 = time.time()
    try:
        out = fn()
        out.update(check=name, ok=out.get("status", 200) == 200)
    except Exception as e:  # noqa: BLE001
        out = {"check": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    out["seconds"] = round(time.time() - t0, 2)
    return out


def nse_session():
    s = requests.Session()
    s.headers.update(UA)
    h = s.get(NSE_HOME, timeout=15)
    return s, h.status_code


def nse_announcements():
    s, home = nse_session()
    r = s.get(f"{NSE_HOME}/api/corporate-announcements?index=equities&symbol=RELIANCE",
              timeout=20, headers={"Referer": NSE_HOME})
    items = r.json() if r.ok else []
    syms = Counter(i.get("symbol") for i in items) if isinstance(items, list) else {}
    return {"home_status": home, "status": r.status_code, "items": len(items),
            "reliance_items": syms.get("RELIANCE", 0),
            "distinct_symbols": len(syms), "top_symbols": syms.most_common(4),
            "first_item_keys": sorted(items[0].keys())[:12] if items else None,
            "newest": items[0].get("an_dt") if items else None}


def nse_json(path):
    def run():
        s, home = nse_session()
        r = s.get(f"{NSE_HOME}/api/{path}", timeout=20, headers={"Referer": NSE_HOME})
        data = r.json() if r.ok else None
        n = len(data) if isinstance(data, (list, dict)) else None
        return {"home_status": home, "status": r.status_code, "items": n}
    return run


def nse_csv(url):
    def run():
        r = requests.get(url, headers=UA, timeout=30)
        return {"status": r.status_code, "bytes": len(r.content),
                "lines": r.text.count("\n") if r.ok else None}
    return run


def yahoo(host):
    def run():
        r = requests.get(f"https://{host}/v8/finance/chart/RELIANCE.NS?range=5d&interval=1d",
                         headers=UA, timeout=20)
        closes = []
        if r.ok:
            res = r.json()["chart"]["result"][0]
            closes = [round(c, 2) for c in res["indicators"]["quote"][0]["close"] if c]
        return {"status": r.status_code, "closes": closes,
                "body_start": None if r.ok else r.text[:120]}
    return run


def groq_chat():
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set in Vercel env")
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": f"Bearer {key}"}, timeout=30,
                      json={"model": "openai/gpt-oss-120b", "max_completion_tokens": 64,
                            "messages": [{"role": "user", "content": "Reply with the word ok."}]})
    return {"status": r.status_code,
            "rate_limit_headers": {k: v for k, v in r.headers.items()
                                   if k.lower().startswith(("x-ratelimit", "retry-after"))},
            "error": None if r.ok else r.text[:200]}


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/spike")
def spike(token: str = Query(""), date: str = Query("2026-11-04")):
    expected = os.environ.get("SPIKE_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    ymd = date.replace("-", "")
    arch = "https://nsearchives.nseindia.com/content/"
    return {
        "vercel_region": os.environ.get("VERCEL_REGION"),
        "checks": [
            timed("nse_announcements", nse_announcements),
            timed("nse_event_calendar", nse_json("event-calendar?index=equities&symbol=RELIANCE")),
            timed("nse_fii_dii", nse_json("fiidiiTradeReact")),
            timed("nse_bulk_snapshot", nse_json("snapshot-capital-market-largedeal")),
            timed("nse_equity_list", nse_csv(arch + "equities/EQUITY_L.csv")),
            timed("nse_bhavcopy_" + ymd,
                  nse_csv(arch + f"cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip")),
            timed("yahoo_query1", yahoo("query1.finance.yahoo.com")),
            timed("yahoo_query2", yahoo("query2.finance.yahoo.com")),
            timed("groq_chat", groq_chat),
        ],
    }
