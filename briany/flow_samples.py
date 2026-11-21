# SPDX-License-Identifier: Apache-2.0
"""Can the bot answer FII/DII and bulk/block-deal questions, and for which days? Capture the candidate NSE sources.

  python -m briany.flow_samples --end 2026-11-20
Public data only; no secret is used. What is KNOWN (real samples of 6 Nov 2026): fiidiiTradeReact and
snapshot-capital-market-largedeal return the latest day only (the saved fixture of the snapshot was cut to 25 rows per list by
the sampling tool, so its row counts say nothing about NSE's completeness; this tool compares NSE's declared counts with the
rows actually listed). Everything else below is an UNVERIFIED candidate: this tool finds out whether it works, how many rows a
day it gives, whether it is complete, how far back it goes and how many bytes a day costs.
Writes samples/flows/* (raw copies, capped) and samples/flows/summary.txt.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date as Date, timedelta
from pathlib import Path

API = "https://www.nseindia.com/api/"
ARCHIVE = "https://nsearchives.nseindia.com/content/equities/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/csv, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
RAW_CAP = 400_000                  # raw characters kept per source
HIST = "historicalOR/bulk-block-short-deals?optionType={kind}&from={f}&to={t}"


def nse_date(d: Date) -> str:
    return f"{d.day:02d}-{d.month:02d}-{d.year}"


def candidates(end: Date) -> list:
    """(name, url, note). Windows end on `end`: one week, and three months to see how deep history goes."""
    week, quarter = nse_date(end - timedelta(days=7)), nse_date(end - timedelta(days=90))
    to = nse_date(end)
    return [
        ("fii_dii_api", API + "fiidiiTradeReact", "known: latest day only"),
        ("fii_dii_nse_api", API + "fiidiiTradeNse", "candidate"),
        ("largedeal_snapshot_api", API + "snapshot-capital-market-largedeal", "known: latest day, truncated lists"),
        ("bulk_csv", ARCHIVE + "bulk.csv", "candidate archive file"),
        ("block_csv", ARCHIVE + "block.csv", "candidate archive file"),
        ("short_csv", ARCHIVE + "short_selling.csv", "candidate archive file"),
        ("hist_bulk_week", API + HIST.format(kind="bulk_deals", f=week, t=to), "candidate: history by date range"),
        ("hist_block_week", API + HIST.format(kind="block_deals", f=week, t=to), "candidate: history by date range"),
        ("hist_short_week", API + HIST.format(kind="short_selling", f=week, t=to), "candidate: history by date range"),
        ("hist_bulk_3m", API + HIST.format(kind="bulk_deals", f=quarter, t=to), "candidate: how deep, how big"),
    ]


def fetch(url: str, timeout: int = 40) -> dict:
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "seconds": round(time.time() - t0, 2), "error": "HTTPError"}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "seconds": round(time.time() - t0, 2), "error": type(e).__name__}
    return {"status": status, "seconds": round(time.time() - t0, 2), "bytes": len(body), "text": body.decode("utf-8", "replace")}


# ------------------------------------------------------------------ describing what came back
DATE_KEY = re.compile(r"date", re.I)


def _records(node, path=""):
    """Every list of dicts in a JSON value, as (path, list)."""
    out = []
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            out.append((path or "$", node))
        for i, x in enumerate(node[:3]):
            out += _records(x, f"{path}[{i}]")
    elif isinstance(node, dict):
        for k, v in node.items():
            out += _records(v, f"{path}.{k}" if path else k)
    return out


def _per_date(rows) -> str:
    keys = [k for k in (rows[0] if rows else {}) if DATE_KEY.search(k)]
    if not keys:
        return "no date column"
    key = keys[0]
    c = Counter(str(r.get(key, ""))[:11] for r in rows)
    shown = sorted(c.items(), key=lambda kv: kv[0])
    return f"{key}: {len(c)} distinct days; rows per day " + ", ".join(f"{d}={n}" for d, n in shown[:12]) + (" ..." if len(shown) > 12 else "")


def describe(text: str) -> list:
    """Plain-language lines about a response body: JSON or CSV, row counts, dates, declared-versus-listed counts."""
    head = text.lstrip()[:1]
    if head and head in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return ["looks like JSON but does not parse"]
        lines = ["JSON"]
        if isinstance(data, dict):
            scalars = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
            lines.append(f"top-level keys: {list(data)[:15]}; scalar fields: {json.dumps(scalars)[:300]}")
        if isinstance(data, dict):                      # a count NSE declares next to a list: are all the rows there?
            for k, v in data.items():
                base = k[:-5] if k.endswith("_DATA") else None
                if isinstance(v, list) and base in data and str(data[base]).strip().isdigit():
                    declared, listed = int(str(data[base]).strip()), len(v)
                    lines.append(f"{base}: declared {declared}, listed {listed} " +
                                 ("(complete)" if declared == listed else f"(INCOMPLETE: {declared - listed} missing)"))
        for path, rows in sorted(_records(data), key=lambda x: -len(x[1]))[:4]:
            lines.append(f"list {path}: {len(rows)} rows; keys {list(rows[0])[:14]}")
            lines.append(f"   {_per_date(rows)}")
            lines.append(f"   first row: {json.dumps(rows[0])[:260]}")
        if not _records(data):
            lines.append(f"no list of records; value: {json.dumps(data)[:300]}")
        return lines
    if head == "<":
        return ["HTML (an error or block page, not data): " + re.sub(r"\s+", " ", text[:160])]
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return ["empty"]
    header = [h.strip() for h in rows[0]]
    recs = [dict(zip(header, r)) for r in rows[1:] if any(c.strip() for c in r)]
    lines = [f"CSV: {len(recs)} rows; columns {header}", f"   {_per_date(recs)}"]
    if recs:
        lines.append(f"   first row: {json.dumps(recs[0])[:260]}")
    return lines


def run(end: str, out_dir: str = "samples/flows", fetcher=fetch, pause: float = 1.5) -> str:
    try:
        end_day = Date.fromisoformat(end)
    except ValueError:
        raise SystemExit(f"bad date {end!r}: use YYYY-MM-DD") from None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"capture ending {end}; every URL not marked 'known' is an UNVERIFIED candidate"]
    for name, url, note in candidates(end_day):
        res = fetcher(url)
        lines.append(f"\n== {name} ({note}): HTTP {res.get('status')} in {res.get('seconds')}s, {res.get('bytes', 0)} bytes")
        lines.append(f"   url: {url}")
        if "text" in res:
            ext = "json" if res["text"].lstrip()[:1] and res["text"].lstrip()[:1] in "{[" else "csv" if "," in res["text"][:500] else "txt"
            (out / f"{name}.{ext}").write_text(res["text"][:RAW_CAP], encoding="utf-8")
            lines += ["   " + x for x in describe(res["text"])]
        else:
            lines.append(f"   failed: {res.get('error')}")
        time.sleep(pause)
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description="Capture the candidate FII/DII and deal sources.")
    ap.add_argument("--end", default="2026-11-20", help="last trading day to ask for (YYYY-MM-DD)")
    ap.add_argument("--out", default="samples/flows")
    a = ap.parse_args(argv)
    print(run(a.end, a.out))


if __name__ == "__main__":
    main()
