"""Step 0 hosting spike: can a Vercel function reach NSE, Yahoo and Groq?
Throwaway. Open: https://<your-app>.vercel.app/spike?token=<SPIKE_TOKEN>
"""
import csv
import hmac
import io
import os
import time
import zipfile
from datetime import datetime, timedelta
from collections import Counter

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request

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


# ------------------------------------------------------------ v3: price sources
def nse_get(path, params=None):
    s, home = nse_session()
    r = s.get(f"{NSE_HOME}/api/{path}", params=params, timeout=25,
              headers={"Referer": NSE_HOME})
    return r, home


def probe_quote():
    r, home = nse_get("quote-equity", {"symbol": "RELIANCE"})
    d = r.json() if r.ok else {}
    pi = d.get("priceInfo", {}) or {}
    return {"home_status": home, "status": r.status_code, "top_keys": list(d.keys())[:10],
            "lastPrice": pi.get("lastPrice"), "previousClose": pi.get("previousClose"),
            "pChange": pi.get("pChange"),
            "lastUpdateTime": (d.get("metadata", {}) or {}).get("lastUpdateTime")}


def probe_historical(date):
    end = datetime.strptime(date, "%Y-%m-%d")
    start = end - timedelta(days=6)
    r, home = nse_get("historical/cm/equity",
                      {"symbol": "RELIANCE", "series": '["EQ"]',
                       "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y")})
    d = r.json() if r.ok else {}
    rows = d.get("data", []) if isinstance(d, dict) else []
    return {"home_status": home, "status": r.status_code, "rows": len(rows),
            "first_row": rows[0] if rows else None, "last_row": rows[-1] if rows else None}


def probe_ann_dates(date):
    end = datetime.strptime(date, "%Y-%m-%d")
    start = end - timedelta(days=7)
    r, home = nse_get("corporate-announcements",
                      {"index": "equities", "symbol": "RELIANCE",
                       "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")})
    items = r.json() if r.ok else []
    return {"home_status": home, "status": r.status_code,
            "items": len(items) if isinstance(items, list) else None,
            "oldest": items[-1].get("an_dt") if items else None,
            "newest": items[0].get("an_dt") if items else None}


def probe_indices():
    r, home = nse_get("allIndices")
    d = r.json() if r.ok else {}
    rows = d.get("data", []) if isinstance(d, dict) else []
    nifty = next((x for x in rows if x.get("index") == "NIFTY 50"), None)
    return {"home_status": home, "status": r.status_code, "indices": len(rows),
            "nifty": {k: nifty.get(k) for k in ("last", "percentChange", "previousClose")} if nifty else None}


def probe_bhav(date):
    ymd = date.replace("-", "")
    r = requests.get("https://nsearchives.nseindia.com/content/cm/"
                     f"BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip", headers=UA, timeout=30)
    if not r.ok:
        return {"status": r.status_code}
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rows = list(csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")))
    hit = [x for x in rows if x.get("TckrSymb") == "RELIANCE" and x.get("SctySrs") == "EQ"]
    return {"status": r.status_code, "rows": len(rows),
            "columns": list(rows[0].keys())[:30] if rows else None,
            "reliance": {k: hit[0].get(k) for k in ("TradDt", "ClsPric", "PrvsClsPric", "TtlTradgVol")} if hit else None}


@app.get("/spike2")
def spike2(token: str = Query(""), date: str = Query("2026-11-04")):
    expected = os.environ.get("SPIKE_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    return {"vercel_region": os.environ.get("VERCEL_REGION"),
            "checks": [timed("nse_quote", probe_quote),
                       timed("nse_historical", lambda: probe_historical(date)),
                       timed("nse_ann_date_filter", lambda: probe_ann_dates(date)),
                       timed("nse_all_indices", probe_indices),
                       timed("bhavcopy_parse", lambda: probe_bhav(date))]}


# ------------------------------------------------------------ step 0b: webhook echo
@app.post("/telegram")
async def telegram(request: Request,
                   x_telegram_bot_api_secret_token: str = Header("")):
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(x_telegram_bot_api_secret_token, secret):
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or ""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if chat_id and text and token:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=10,
                      json={"chat_id": chat_id,
                            "text": f"Echo: {text[:200]}\nRegion: {os.environ.get('VERCEL_REGION')}"})
    return {"ok": True}


# ------------------------------------------------------------ step 0c: storage test
def redis_cmd(*args):
    """Use the REST API if its env vars exist, else a TCP connection (redis-py)."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    tok = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and tok:
        r = requests.post(url, headers={"Authorization": f"Bearer {tok}"},
                          json=list(args), timeout=15)
        r.raise_for_status()
        return r.json().get("result")
    conn = os.environ.get("KV_REDIS_URL") or os.environ.get("REDIS_URL")
    if not conn:
        raise RuntimeError("no Redis env vars found (REST or TCP)")
    import redis
    client = redis.Redis.from_url(conn, socket_timeout=10, socket_connect_timeout=10,
                                  decode_responses=True)
    return client.execute_command(*args)


def redis_roundtrip():
    stamp = str(time.time())
    redis_cmd("SET", "spike:ping", stamp, "EX", 300)
    got = redis_cmd("GET", "spike:ping")
    if got != stamp:
        raise RuntimeError("value mismatch")
    return {"status": 200, "roundtrip": "ok"}


def redis_big_hash():
    # ~2,000 symbol->close pairs in ONE command, like one day's closes
    args = ["HSET", "spike:day"]
    for i in range(2000):
        args += [f"SYM{i:04d}", f"{1000 + i / 7:.2f}"]
    n = redis_cmd(*args)
    size = redis_cmd("HLEN", "spike:day")
    redis_cmd("EXPIRE", "spike:day", 300)
    return {"status": 200, "fields_added": n, "hlen": size,
            "payload_bytes": len(str(args))}


@app.get("/storage")
def storage(token: str = Query("")):
    expected = os.environ.get("SPIKE_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    names = sorted(k for k in os.environ
                   if k.startswith(("UPSTASH", "KV_", "REDIS")))
    return {"vercel_region": os.environ.get("VERCEL_REGION"),
            "redis_env_var_names": names,
            "checks": [timed("redis_set_get", redis_roundtrip),
                       timed("redis_big_hash", redis_big_hash)]}
