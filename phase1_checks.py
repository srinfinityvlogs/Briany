#!/usr/bin/env python3
"""
Phase 1 feasibility checks (throwaway, read-only, personal use).

Run each suite separately, on your machine AND on Northflank:
    python phase1_checks.py env
    python phase1_checks.py symbols
    python phase1_checks.py prices
    python phase1_checks.py accuracy --date 2026-10-30
    python phase1_checks.py announcements
    python phase1_checks.py news
    python phase1_checks.py events      # suite E: results calendar, results, corporate actions
    python phase1_checks.py deals       # suite F: bulk/block deals, FII/DII, insider, pledge

Every check records PASS / FAIL / ERROR and never crashes the run.
Results are appended to results/<label>.jsonl (label = --label, default "local").
NOTE: endpoint URLs below are from memory/tools docs and are UNVERIFIED.
A FAIL here is itself a useful finding, not necessarily a bug.
"""
import argparse, io, json, os, platform, sys, time, zipfile, difflib
from datetime import datetime, timedelta, timezone

import requests

UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# Confirm these tickers with the `symbols` suite before trusting them.
YF_TICKERS = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "TEMPSENS.NS", "KRN.NS",
              "GOLDBEES.NS", "^NSEI"]
NSE_SYMBOLS = ["RELIANCE", "TCS", "INFY", "TEMPSENS", "KRN", "GOLDBEES"]
NEWS_QUERIES = ["Reliance Industries", "Tempsens Instruments", "KRN Heat Exchanger",
                "GoldBees Nippon gold ETF"]
NAME_QUERIES = ["Tempsens", "KRN", "reliance", "relience", "tempsens india", "goldbees"]

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BHAV_URL = ("https://nsearchives.nseindia.com/content/cm/"
            "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip")
NSE_HOME = "https://www.nseindia.com"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}"
BSE_ANN = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
           "?pageno=1&strCat=-1&strPrevDate={d0}&strScrip=&strSearch=P&strToDate={d1}&strType=C")
BSE_HEADERS = dict(UA, Referer="https://www.bseindia.com/", Origin="https://www.bseindia.com")

RESULTS, LABEL = [], "local"


def record(suite, name, status, detail="", **extra):
    row = {"ts": datetime.now(timezone.utc).isoformat(), "env": LABEL, "suite": suite,
           "check": name, "status": status, "detail": str(detail)[:500], **extra}
    RESULTS.append(row)
    print(f"[{status:5}] {suite}/{name}: {str(detail)[:160]}")


def safe(suite, name):
    """Decorator-less helper: run a callable, record ERROR instead of raising."""
    def run(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            record(suite, name, "ERROR", f"{type(e).__name__}: {e}")
    return run


def nse_session():
    s = requests.Session()
    s.headers.update(UA)
    s.get(NSE_HOME, timeout=15)  # obtains cookies the API endpoints expect
    return s


# ---------------------------------------------------------------- env
def suite_env(_a):
    def ip():
        r = requests.get("https://api.ipify.org?format=json", timeout=10)
        record("env", "public_ip", "INFO", r.json().get("ip"),
               platform=platform.platform(), python=sys.version.split()[0])
    safe("env", "public_ip")(ip)

    for name, url, hdr in [("nse_home", NSE_HOME, UA),
                           ("nse_equity_list", EQUITY_LIST_URL, UA),
                           ("bse_api", BSE_ANN.format(d0="20260101", d1="20260101"), BSE_HEADERS),
                           ("yahoo_chart", "https://query1.finance.yahoo.com/v8/finance/chart/TCS.NS", UA)]:
        def probe(name=name, url=url, hdr=hdr):
            r = requests.get(url, headers=hdr, timeout=20)
            ok = r.status_code == 200
            record("env", name, "PASS" if ok else "WARN", f"HTTP {r.status_code} (raw probe; real data paths may differ)",
                   http=r.status_code, bytes=len(r.content))
        safe("env", name)(probe)


# ---------------------------------------------------------------- A symbols
def load_equity_list():
    r = requests.get(EQUITY_LIST_URL, headers=UA, timeout=30)
    r.raise_for_status()
    import pandas as pd
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    return df


def suite_symbols(_a):
    def run():
        df = load_equity_list()
        names = df["NAME OF COMPANY"].str.lower().tolist()
        syms = df["SYMBOL"].str.lower().tolist()
        record("symbols", "equity_list", "PASS", f"{len(df)} listed symbols loaded")
        for q in NAME_QUERIES:
            ql = q.lower()
            exact = [i for i, s in enumerate(syms) if s == ql]
            pool = difflib.get_close_matches(ql, names + syms, n=5, cutoff=0.4)
            hits = []
            for p in pool:
                i = names.index(p) if p in names else syms.index(p)
                hits.append(f"{df.iloc[i]['SYMBOL']} ({df.iloc[i]['NAME OF COMPANY']})")
            hits = list(dict.fromkeys(([f"{df.iloc[exact[0]]['SYMBOL']} (exact symbol)"]
                                       if exact else []) + hits))[:4]
            # REVIEW MANUALLY: is the intended company in the candidates?
            record("symbols", f"resolve:{q}", "INFO" if hits else "FAIL", " | ".join(hits))
    safe("symbols", "equity_list")(run)


# ---------------------------------------------------------------- B prices
def suite_prices(_a):
    import yfinance as yf
    for t in YF_TICKERS:
        def run(t=t):
            h = yf.Ticker(t).history(period="2y", interval="1d", auto_adjust=True)
            if h is None or h.empty:
                record("prices", t, "FAIL", "no data returned")
                return
            last = h.index[-1]
            age = (datetime.now(timezone.utc).date() - last.date()).days
            c = h["Close"]

            def ret(days):
                past = c[c.index <= last - timedelta(days=days)]
                return None if past.empty else round((c.iloc[-1] / past.iloc[-1] - 1) * 100, 2)
            missing_vol = int((h["Volume"] == 0).sum()) if "Volume" in h else -1
            status = "PASS" if age <= 4 and len(h) >= 20 else "FAIL"
            short = " SHORT_HISTORY(new listing?)" if len(h) < 200 else ""
            record("prices", t, status,
                   f"rows={len(h)} first={h.index[0].date()} last={last.date()} age_days={age} "
                   f"close={c.iloc[-1]:.2f} zero_vol_days={missing_vol}{short}",
                   ret_1m=ret(30), ret_3m=ret(90), ret_1y=ret(365))
        safe("prices", t)(run)


# ---------------------------------------------------------------- C accuracy
def suite_accuracy(a):
    """Compare Yahoo (unadjusted) closes with NSE's official end-of-day file.
    --date accepts one or several comma-separated past trading days."""
    import pandas as pd
    import yfinance as yf
    dates = [datetime.strptime(x.strip(), "%Y-%m-%d") for x in a.date.split(",") if x.strip()]
    cache = {}

    def yahoo_window(sym):
        # One wide-window request per symbol; pick the date afterwards.
        if sym not in cache:
            lo, hi = min(dates) - timedelta(days=7), max(dates) + timedelta(days=4)
            cache[sym] = yf.Ticker(f"{sym}.NS").history(start=lo, end=hi, auto_adjust=False)
        return cache[sym]

    for d in dates:
        ds = d.strftime("%Y-%m-%d")

        def run(d=d, ds=ds):
            r = requests.get(BHAV_URL.format(ymd=d.strftime("%Y%m%d")), headers=UA, timeout=40)
            if r.status_code != 200:
                record("accuracy", f"bhavcopy:{ds}", "FAIL",
                       f"HTTP {r.status_code} (holiday/weekend or URL changed?)")
                return
            z = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            record("accuracy", f"bhavcopy:{ds}", "PASS", f"{len(df)} rows")
            for sym in NSE_SYMBOLS:
                row = df[df["TckrSymb"] == sym]
                if row.empty:
                    record("accuracy", f"{sym}:{ds}", "FAIL", "symbol not in bhavcopy")
                    continue
                official = float(row.iloc[0]["ClsPric"])
                try:
                    h = yahoo_window(sym)
                except Exception as e:  # noqa: BLE001
                    record("accuracy", f"{sym}:{ds}", "ERROR", f"yahoo {type(e).__name__}: {e}")
                    continue
                day = h[h.index.strftime("%Y-%m-%d") == ds] if not h.empty else h
                if day.empty:
                    have = [x.strftime("%m-%d") for x in h.index[-8:]] if not h.empty else "no rows at all"
                    record("accuracy", f"{sym}:{ds}", "FAIL",
                           f"official={official}; yahoo has no bar for this date; yahoo dates={have}")
                    continue
                y = float(day["Close"].iloc[0])
                diff = abs(y - official) / official * 100
                record("accuracy", f"{sym}:{ds}", "PASS" if diff <= 0.1 else "FAIL",
                       f"official={official} yahoo={y:.2f} diff={diff:.3f}%", diff_pct=round(diff, 4))
        safe("accuracy", f"bhavcopy:{ds}")(run)
    print("\nAlso verify 3 of these by eye against your broker app / exchange site and keep screenshots.")


# ---------------------------------------------------------------- D announcements
def suite_announcements(_a):
    def nse():
        s = nse_session()
        for sym in NSE_SYMBOLS:
            def one(sym=sym):
                r = s.get(NSE_ANN.format(sym=sym), timeout=20, headers={"Referer": NSE_HOME})
                if r.status_code != 200:
                    record("announcements", f"nse:{sym}", "FAIL", f"HTTP {r.status_code}")
                    return
                data = r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                latest = [f"{i.get('an_dt') or i.get('sort_date')}: {(i.get('desc') or '')[:50]}"
                          for i in items[:3]]
                cutoff = datetime.now() - timedelta(days=7)
                recent = 0
                for i in items:
                    try:
                        if datetime.strptime(i.get("an_dt", ""), "%d-%b-%Y %H:%M:%S") >= cutoff:
                            recent += 1
                    except ValueError:
                        pass
                record("announcements", f"nse:{sym}", "PASS" if items else "FAIL",
                       f"{len(items)} items total, {recent} in last 7 days; latest: {latest}",
                       recent_7d=recent)
                time.sleep(1.5)
            safe("announcements", f"nse:{sym}")(one)
    safe("announcements", "nse_session")(nse)

    def bse():
        today = datetime.now()
        r = requests.get(BSE_ANN.format(d0=(today - timedelta(days=2)).strftime("%Y%m%d"),
                                        d1=today.strftime("%Y%m%d")),
                         headers=BSE_HEADERS, timeout=25)
        if r.status_code != 200:
            record("announcements", "bse:all_2d", "FAIL", f"HTTP {r.status_code}")
            return
        rows = r.json().get("Table", [])
        sample = [f"{x.get('SLONGNAME')}: {(x.get('NEWSSUB') or '')[:40]}" for x in rows[:3]]
        record("announcements", "bse:all_2d", "PASS" if rows else "FAIL",
               f"{len(rows)} rows (page 1); sample: {sample}")
    safe("announcements", "bse:all_2d")(bse)
    print("\nCompare 3 results per exchange with the website for the same stock. "
          "Every filing visible on the site for the last 7 days should appear here.")


# ---------------------------------------------------------------- G news
def suite_news(_a):
    import feedparser
    from urllib.parse import quote
    for q in NEWS_QUERIES:
        def run(q=q):
            url = (f"https://news.google.com/rss/search?q={quote(q + ' when:7d')}"
                   "&hl=en-IN&gl=IN&ceid=IN:en")
            f = feedparser.parse(url)
            n = len(f.entries)
            titles = [e.title[:70] for e in f.entries[:3]]
            record("news", q, "PASS" if n >= 3 else "FAIL", f"{n} items; sample: {titles}")
        safe("news", q)(run)
    print("\nManually judge relevance: are these really about the company, and dated within 7 days?")



# ---------------------------------------------------------------- shared NSE helper
def summarize(data, n=2):
    """Return (count, label, first_items) for a JSON payload of unknown shape."""
    if isinstance(data, dict):
        lists = {k: v for k, v in data.items() if isinstance(v, list)}
        if lists:
            k = max(lists, key=lambda x: len(lists[x]))
            return len(lists[k]), f"key={k}", lists[k][:n]
        return (1 if data else 0), f"keys={list(data)[:8]}", ([data] if data else [])
    if isinstance(data, list):
        return len(data), "list", data[:n]
    return 0, type(data).__name__, []


def nse_probe(sess, suite, name, url, trim=230, want_items=True):
    """GET an NSE JSON endpoint; record status, item count and a short sample.
    Returns parsed JSON or None. Never raises."""
    try:
        r = sess.get(url, timeout=25, headers={"Referer": NSE_HOME})
        if r.status_code != 200:
            record(suite, name, "FAIL", f"HTTP {r.status_code}")
            return None
        try:
            data = r.json()
        except ValueError:
            record(suite, name, "ERROR", f"non-JSON response: {r.text[:100]!r}")
            return None
        count, label, first = summarize(data)
        sample = json.dumps(first[0], ensure_ascii=False)[:trim] if first else "-"
        ok = count > 0 or not want_items
        record(suite, name, "PASS" if ok else "FAIL", f"{count} items ({label}); first: {sample}",
               items=count)
        return data
    except Exception as e:  # noqa: BLE001
        record(suite, name, "ERROR", f"{type(e).__name__}: {e}")
        return None
    finally:
        time.sleep(1.5)


# ---------------------------------------------------------------- E events and results
E_SYMBOLS = ["RELIANCE", "TCS", "KRN", "TEMPSENS"]


def suite_events(_a):
    try:
        s = nse_session()
    except Exception as e:  # noqa: BLE001
        record("events", "nse_session", "ERROR", f"{type(e).__name__}: {e}")
        s = None
    if s:
        base = "https://www.nseindia.com/api/"
        for sym in E_SYMBOLS:
            cal = nse_probe(s, "events", f"event_calendar:{sym}",
                            f"{base}event-calendar?index=equities&symbol={sym}")
            # Extract the next results-related event on/after today, if any.
            try:
                items = cal if isinstance(cal, list) else []
                upcoming = []
                for it in items:
                    text = json.dumps(it).lower()
                    if "result" in text:
                        d = str(it.get("date", ""))
                        try:
                            dt = datetime.strptime(d, "%d-%b-%Y")
                        except ValueError:
                            continue
                        if dt.date() >= datetime.now().date():
                            upcoming.append((dt, it.get("purpose", "")))
                if upcoming:
                    upcoming.sort()
                    record("events", f"next_results:{sym}", "INFO",
                           f"{upcoming[0][0].date()} - {upcoming[0][1]}  (VERIFY on the NSE/company site)")
                else:
                    record("events", f"next_results:{sym}", "INFO",
                           "no upcoming results event found in calendar (may be normal)")
            except Exception as e:  # noqa: BLE001
                record("events", f"next_results:{sym}", "ERROR", f"{type(e).__name__}: {e}")
            nse_probe(s, "events", f"fin_results:{sym}",
                      f"{base}corporates-financial-results?index=equities&symbol={sym}&period=Quarterly")
            nse_probe(s, "events", f"corp_actions:{sym}",
                      f"{base}corporates-corporateActions?index=equities&symbol={sym}",
                      want_items=False)
            nse_probe(s, "events", f"board_meetings:{sym}",
                      f"{base}corporate-board-meetings?index=equities&symbol={sym}")
    # Yahoo's own calendar as a second opinion for the next earnings date
    try:
        import yfinance as yf
        for sym in ["RELIANCE", "KRN"]:
            try:
                cal = yf.Ticker(f"{sym}.NS").calendar
                record("events", f"yahoo_calendar:{sym}", "INFO" if cal else "FAIL", str(cal)[:200])
            except Exception as e:  # noqa: BLE001
                record("events", f"yahoo_calendar:{sym}", "ERROR", f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        record("events", "yahoo_import", "ERROR", str(e))
    print("\nVerify each next-results date and the latest quarter's revenue/profit against the "
          "company's filing. Quote nothing from memory.")


# ---------------------------------------------------------------- F deals and flows
def suite_deals(_a):
    # 1) Archive CSVs on the host that has worked from the start (no cookies needed)
    for name, url in [("bulk_csv", "https://nsearchives.nseindia.com/content/equities/bulk.csv"),
                      ("block_csv", "https://nsearchives.nseindia.com/content/equities/block.csv")]:
        def run(name=name, url=url):
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code != 200:
                record("deals", name, "FAIL", f"HTTP {r.status_code}")
                return
            lines = r.text.strip().splitlines()
            record("deals", name, "PASS" if len(lines) > 1 else "FAIL",
                   f"{max(len(lines) - 1, 0)} data rows; header: {lines[0][:120] if lines else '-'}; "
                   f"first: {lines[1][:140] if len(lines) > 1 else '-'}")
        safe("deals", name)(run)

    # 2) Cookie-based JSON endpoints
    try:
        s = nse_session()
    except Exception as e:  # noqa: BLE001
        record("deals", "nse_session", "ERROR", f"{type(e).__name__}: {e}")
        return
    base = "https://www.nseindia.com/api/"
    big = nse_probe(s, "deals", "largedeal_snapshot", f"{base}snapshot-capital-market-largedeal")
    if isinstance(big, dict):
        for key in ("BULK_DEALS_DATA", "BLOCK_DEALS_DATA", "SHORT_DEALS_DATA"):
            rows = big.get(key) or []
            mine = [r for r in rows if str(r.get("symbol", "")).upper() in
                    {x for x in NSE_SYMBOLS}]
            record("deals", f"snapshot:{key}", "INFO",
                   f"{len(rows)} rows; rows for our test symbols: {len(mine)}")
    nse_probe(s, "deals", "fii_dii", f"{base}fiidiiTradeReact")
    for sym in ["RELIANCE", "KRN", "TEMPSENS"]:
        nse_probe(s, "deals", f"insider_pit:{sym}",
                  f"{base}corporates-pit?index=equities&symbol={sym}", want_items=False)
        nse_probe(s, "deals", f"pledge:{sym}",
                  f"{base}corporate-pledgedata?index=equities&symbol={sym}", want_items=False)
    print("\nFor FII/DII and bulk deals, compare the latest figures with NSE's website. "
          "Note whether the data is for today or a previous session.")


SUITES = {"env": suite_env, "symbols": suite_symbols, "prices": suite_prices,
          "accuracy": suite_accuracy, "announcements": suite_announcements, "news": suite_news,
          "events": suite_events, "deals": suite_deals}


def main():
    global LABEL
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("suite", choices=list(SUITES) + ["all"])
    p.add_argument("--label", default="local", help="environment label, e.g. local or northflank")
    p.add_argument("--date", default=(datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
                   help="past trading day for the accuracy suite (YYYY-MM-DD)")
    a = p.parse_args()
    LABEL = a.label
    for name in (SUITES if a.suite == "all" else [a.suite]):
        print(f"\n=== {name} ({LABEL}) ===")
        SUITES[name](a)
    os.makedirs("results", exist_ok=True)
    with open(f"results/{LABEL}.jsonl", "a", encoding="utf-8") as fh:
        for r in RESULTS:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nSaved {len(RESULTS)} rows to results/{LABEL}.jsonl")


if __name__ == "__main__":
    main()
