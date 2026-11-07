"""Capture the real shape of NSE's JSON endpoints (public data only, no secrets).

  python -m briany.nse_samples
Writes truncated samples to samples/<name>.json and prints a compact summary.
Used once, to write the step 4 parsers against real responses instead of guesses.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://www.nseindia.com/api/"
ARCHIVE = "https://nsearchives.nseindia.com/content/equities/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# (name, path under /api/)  -- dates are dd-mm-yyyy
SAMPLES = [
    ("corp_actions_symbol", "corporates-corporateActions?index=equities&symbol=RELIANCE"),
    ("corp_actions_range_week", "corporates-corporateActions?index=equities&from_date=01-11-2026&to_date=14-11-2026"),
    ("corp_actions_range_year", "corporates-corporateActions?index=equities&from_date=01-11-2025&to_date=06-11-2026"),
    ("announcements_window", "corporate-announcements?index=equities&symbol=RELIANCE&from_date=29-10-2026&to_date=06-11-2026"),
    ("event_calendar", "event-calendar?index=equities&symbol=RELIANCE"),
    ("board_meetings", "corporate-board-meetings?index=equities&symbol=RELIANCE"),
    ("results_quarterly", "corporates-financial-results?index=equities&symbol=RELIANCE&period=Quarterly"),
    ("insider_pit", "corporates-pit?index=equities&symbol=RELIANCE"),
    ("fii_dii", "fiidiiTradeReact"),
    ("bulk_snapshot", "snapshot-capital-market-largedeal"),
]
CSV_SAMPLES = [("bulk_csv", ARCHIVE + "bulk.csv"), ("block_csv", ARCHIVE + "block.csv")]


def truncate(obj, max_str=300, max_items=25):
    """Shrink nested JSON for sampling: long strings and long lists are cut."""
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + f"...[{len(obj)} chars]"
    if isinstance(obj, list):
        out = [truncate(x, max_str, max_items) for x in obj[:max_items]]
        if len(obj) > max_items:
            out.append(f"...[{len(obj) - max_items} more items]")
        return out
    if isinstance(obj, dict):
        return {k: truncate(v, max_str, max_items) for k, v in obj.items()}
    return obj


def describe(data) -> str:
    """One-line shape description."""
    if isinstance(data, list):
        keys = sorted(data[0].keys()) if data and isinstance(data[0], dict) else []
        return f"list of {len(data)}; first-record keys: {keys}"
    if isinstance(data, dict):
        inner = {k: len(v) for k, v in data.items() if isinstance(v, list)}
        return f"dict keys: {sorted(data.keys())}; list lengths: {inner}"
    return type(data).__name__


def first_record(data):
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v:
                return v[0]
    return None


def fetch(url, timeout=30):
    """Returns a dict: status, seconds, bytes, and data or error. Never raises."""
    t0 = time.time()
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "seconds": round(time.time() - t0, 2), "error": "HTTPError"}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "seconds": round(time.time() - t0, 2), "error": type(e).__name__}
    res = {"status": status, "seconds": round(time.time() - t0, 2), "bytes": len(body)}
    text = body.decode("utf-8", "replace")
    try:
        res["data"] = json.loads(text)
    except ValueError:
        res["non_json_start"] = text[:200]
    return res


def main(out_dir="samples", pause=1.5):
    os.makedirs(out_dir, exist_ok=True)
    for name, path in SAMPLES:
        res = fetch(BASE + path)
        print(f"\n== {name}: HTTP {res['status']} in {res['seconds']}s, {res.get('bytes', 0)} bytes")
        print("   url:", BASE + path)
        if "data" in res:
            data = res["data"]
            print("   shape:", describe(data))
            rec = first_record(data)
            if rec is not None:
                print("   first record:", json.dumps(truncate(rec, 120), ensure_ascii=False)[:900])
            with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump({"url": BASE + path, "sample": truncate(data)}, fh, indent=1, ensure_ascii=False)
        else:
            print("  ", {k: v for k, v in res.items() if k != "data"})
        time.sleep(pause)
    for name, url in CSV_SAMPLES:
        t0 = time.time()
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                lines = r.read().decode("utf-8", "replace").splitlines()
            print(f"\n== {name}: {len(lines)} lines in {round(time.time() - t0, 2)}s")
            print("   header:", lines[0][:300] if lines else None)
            print("   first row:", lines[1][:300] if len(lines) > 1 else None)
            with open(os.path.join(out_dir, name + ".txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines[:15]))
        except Exception as e:  # noqa: BLE001
            print(f"\n== {name}: {type(e).__name__}")
        time.sleep(pause)


if __name__ == "__main__":
    main()
