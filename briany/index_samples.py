# SPDX-License-Identifier: Apache-2.0
"""Can the bot answer 'where did the Nifty close?'. Capture the candidate NSE index sources and show what they hold.

  python -m briany.index_samples --day 2026-11-18
Public data only; no secret is used. Both candidate URLs are UNVERIFIED guesses: this tool finds out whether they work,
what their columns are, and whether the main indices (with open, close, change and percent change) are in them.
Writes samples/indices/* (raw copies) and samples/indices/summary.txt.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
from pathlib import Path

from briany.name_samples import fetch_text

KEY_INDICES = ("NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY NEXT 50", "NIFTY MIDCAP 100", "INDIA VIX")
CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
API_URL = "https://www.nseindia.com/api/allIndices"


def ddmmyyyy(day: str) -> str:
    try:
        y, m, d = day.split("-")
        if not (len(y) == 4 and len(m) == 2 and len(d) == 2 and (y + m + d).isdigit()):
            raise ValueError
    except ValueError:
        raise SystemExit(f"bad date {day!r}: use YYYY-MM-DD") from None
    return f"{d}{m}{y}"


def describe_csv(text: str):
    """(header, rows as dicts, matching key-index rows)."""
    reader = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not reader:
        return [], [], {}
    header = [h.strip() for h in reader[0]]
    rows = [dict(zip(header, (c.strip() for c in r))) for r in reader[1:] if any(c.strip() for c in r)]
    return header, rows, _matches(rows)


def describe_json(text: str):
    """(keys of the first record, records, matching key-index records) for a JSON body shaped like {'data': [...]}."""
    data = json.loads(text)
    records = data.get("data") if isinstance(data, dict) else data
    records = [r for r in (records or []) if isinstance(r, dict)]
    keys = sorted(records[0]) if records else []
    return keys, records, _matches(records)


def _matches(rows) -> dict:
    found = {}
    for key in KEY_INDICES:
        for r in rows:
            if any(isinstance(v, str) and v.strip().upper() == key for v in r.values()):
                found[key] = r
                break
    return found


def split_days(value: str) -> list:
    """'2026-11-03, 2026-10-30' -> ['2026-11-03', '2026-10-30']; blanks and repeats dropped, order kept."""
    days = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part and part not in days:
            days.append(part)
    return days


def run_days(days, out_dir: str = "samples/indices", fetch=fetch_text, pause: float = 1.5) -> str:
    """Check several days in one run. Each day goes to its own sub-folder (samples/indices/<day>/) with its own summary;
    the live API is fetched only once (with the first day), and a final table lists status, rows and key indices per day."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    blocks, table = [], []
    for i, day in enumerate(days):
        text = run(day, str(out / day), fetch=fetch, pause=pause, with_api=(i == 0))
        blocks.append(f"##### {day}\n{text}")
        head = text.splitlines()[0] if text else ""
        found = [ln for ln in text.splitlines() if "key indices found:" in ln]
        table.append(f"{day}: {head.split(': ', 1)[-1] if head else 'no result'}"
                     + (f" | {found[0].strip()}" if found else ""))
    summary = "\n\n".join(blocks) + "\n\n===== one line per day =====\n" + "\n".join(table)
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    return summary


def run(day: str, out_dir: str = "samples/indices", fetch=fetch_text, pause: float = 1.5, with_api: bool = True) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    sources = [("index_close_csv", CSV_URL.format(ddmmyyyy=ddmmyyyy(day)), "csv")]
    if with_api:
        sources.append(("all_indices_api", API_URL, "json"))
    for name, url, kind in sources:
        res = fetch(url)
        lines.append(f"== {name}: HTTP {res['status']} in {res['seconds']}s, {res.get('bytes', 0)} bytes\n   url: {url}")
        if "text" in res:
            (out / f"{name}.{kind}").write_text(res["text"][:3_000_000], encoding="utf-8")
            try:
                cols, rows, found = describe_csv(res["text"]) if kind == "csv" else describe_json(res["text"])
            except ValueError as e:
                lines.append(f"   not readable as {kind}: {type(e).__name__}; starts: {res['text'][:200]!r}")
                cols, rows, found = [], [], {}
            lines.append(f"   columns: {cols}\n   {len(rows)} rows; key indices found: {sorted(found)}")
            for key, row in found.items():
                lines.append(f"   {key}: {json.dumps(row, ensure_ascii=False)[:400]}")
            if rows and not found:
                lines.append(f"   first row: {json.dumps(rows[0], ensure_ascii=False)[:400]}")
        else:
            lines.append(f"   {res.get('error')}")
        time.sleep(pause)
    summary = "\n".join(lines).rstrip()
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-18",
                    help="a trading day (YYYY-MM-DD); several days may be given, separated by commas")
    ap.add_argument("--out", default="samples/indices")
    a = ap.parse_args(argv)
    days = split_days(a.day)
    if not days:
        ap.error("--day needs at least one date")
    for d in days:
        ddmmyyyy(d)  # a malformed date fails here, before any network call
    print(run(days[0], a.out) if len(days) == 1 else run_days(days, a.out))


if __name__ == "__main__":
    main()
