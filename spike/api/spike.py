"""Step 0 hosting spike: can a Vercel function reach NSE, Yahoo and Groq?
Throwaway. Open: https://<your-app>.vercel.app/api/spike?token=<SPIKE_TOKEN>
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

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
        out.update(check=name, ok=True)
    except Exception as e:  # noqa: BLE001
        out = {"check": name, "ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    out["seconds"] = round(time.time() - t0, 2)
    return out


def nse_announcements():
    s = requests.Session()
    s.headers.update(UA)
    h = s.get(NSE_HOME, timeout=15)
    r = s.get(f"{NSE_HOME}/api/corporate-announcements?index=equities&symbol=RELIANCE",
              timeout=20, headers={"Referer": NSE_HOME})
    items = r.json() if r.ok else []
    return {"home_status": h.status_code, "status": r.status_code,
            "items": len(items) if isinstance(items, list) else "not a list",
            "newest": (items[0].get("an_dt") if items else None)}


def nse_archive_csv():
    r = requests.get("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                     headers=UA, timeout=20)
    return {"status": r.status_code, "bytes": len(r.content)}


def yahoo_chart():
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS"
                     "?range=5d&interval=1d", headers=UA, timeout=20)
    closes = []
    if r.ok:
        res = r.json()["chart"]["result"][0]
        closes = [round(c, 2) for c in res["indicators"]["quote"][0]["close"] if c]
    return {"status": r.status_code, "closes": closes}


def groq_models():
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set in Vercel env")
    r = requests.get("https://api.groq.com/openai/v1/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
    ids = [m["id"] for m in r.json().get("data", [])] if r.ok else []
    return {"status": r.status_code, "has_gpt_oss_120b": "openai/gpt-oss-120b" in ids,
            "rate_limit_headers": {k: v for k, v in r.headers.items()
                                   if k.lower().startswith("x-ratelimit")}}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        expected = os.environ.get("SPIKE_TOKEN", "")
        if not expected or q.get("token", [""])[0] != expected:
            self.send_response(403)
            self.end_headers()
            return
        report = {
            "vercel_region": os.environ.get("VERCEL_REGION"),
            "checks": [timed("nse_announcements", nse_announcements),
                       timed("nse_archive_csv", nse_archive_csv),
                       timed("yahoo_chart", yahoo_chart),
                       timed("groq_models", groq_models)],
        }
        body = json.dumps(report, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
