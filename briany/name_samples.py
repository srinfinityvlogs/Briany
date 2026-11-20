# SPDX-License-Identifier: Apache-2.0
"""Which NSE file gives each stock and ETF its proper company name? Capture the candidates and measure their coverage.

  python -m briany.name_samples --day 2026-11-18
Public data only; no secret is used. The candidate URLs below are UNVERIFIED guesses: this tool exists to find out which
of them work, what their columns are, and how many of the instruments in the price file each one names.
Writes samples/names/*.csv (raw copies) and samples/names/summary.txt.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from briany.nse_samples import ARCHIVE, HEADERS
from briany.nsefiles import fetch_bhavcopy
from briany.symbols import parse_bhavcopy

CANDIDATES = [
    ("equity_list", ARCHIVE + "EQUITY_L.csv"),
    ("etf_list", ARCHIVE + "eq_etfseclist.csv"),
    ("sme_list", ARCHIVE + "SME_EQUITY_L.csv"),
]
KEY_SYMBOLS = ("RELIANCE", "TCS", "INFY", "TEMPSENS", "KRN", "GOLDBEES")


def _norm(column: str) -> str:
    return "".join(ch for ch in column.upper() if ch.isalnum())


def read_names(text: str):
    """(columns, symbol_column, name_column, {symbol: name}) from an NSE list. Columns are guessed from their headers;
    the guess is printed, so a wrong one is visible. Either column is None if no header looks right."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = [c.strip() for c in (reader.fieldnames or [])]
    reader.fieldnames = columns
    symbol_col = next((c for c in columns if _norm(c) == "SYMBOL"), None) or next((c for c in columns if "SYMBOL" in _norm(c)), None)
    name_col = (next((c for c in columns if _norm(c) in ("NAMEOFCOMPANY", "COMPANYNAME", "SECURITYNAME")), None)
                or next((c for c in columns if "NAME" in _norm(c) and c != symbol_col), None))
    names = {}
    if symbol_col and name_col:
        for row in reader:
            sym, name = (row.get(symbol_col) or "").strip(), " ".join((row.get(name_col) or "").split())
            if sym and name:
                names[sym] = name
    return columns, symbol_col, name_col, names


def coverage(instruments, sources: dict) -> dict:
    """How many price-file instruments each source names, and which ones nobody names."""
    named = {}
    for src, names in sources.items():
        named[src] = [i for i in instruments if i.symbol in names]
    union = {i.symbol for found in named.values() for i in found}
    missing = [i for i in instruments if i.symbol not in union]
    return {"total": len(instruments),
            "by_source": {s: len(v) for s, v in named.items()},
            "named_by_any": len(union),
            "missing": missing}


def fetch_text(url: str, timeout: int = 30) -> dict:
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "seconds": round(time.time() - t0, 2), "error": "HTTPError"}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "seconds": round(time.time() - t0, 2), "error": type(e).__name__}
    return {"status": status, "seconds": round(time.time() - t0, 2), "bytes": len(body),
            "text": body.decode("utf-8", "replace")}


def run(day: str, out_dir: str = "samples/names", fetch=fetch_text, bhavcopy=fetch_bhavcopy, pause: float = 1.5) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines, sources = [], {}
    for name, url in CANDIDATES:
        res = fetch(url)
        lines.append(f"== {name}: HTTP {res['status']} in {res['seconds']}s, {res.get('bytes', 0)} bytes\n   url: {url}")
        if "text" in res:
            (out / f"{name}.csv").write_text(res["text"][:3_000_000], encoding="utf-8")
            columns, sym_col, name_col, names = read_names(res["text"])
            lines.append(f"   columns: {columns}\n   symbol column: {sym_col!r}; name column: {name_col!r}; "
                         f"{len(names)} named rows")
            head = res["text"].splitlines()[:3]
            lines.append("   first lines: " + " | ".join(h[:160] for h in head))
            for sym in KEY_SYMBOLS:
                if sym in names:
                    lines.append(f"   {sym}: {names[sym]}")
            if names:
                sources[name] = names
        else:
            lines.append(f"   {res.get('error')}")
        time.sleep(pause)
    csv_text = bhavcopy(day)
    if csv_text is None:
        lines.append(f"\n== price file {day}: not published (holiday or not yet out); coverage not measured")
    else:
        instruments, stats = parse_bhavcopy(csv_text)
        cov = coverage(instruments, sources)
        missing_symbols = {m.symbol for m in cov["missing"]}
        by_series = {}
        for i in instruments:
            by_series.setdefault(i.series, [0, 0])[0] += 1
            by_series[i.series][1] += i.symbol not in missing_symbols
        lines.append(f"\n== coverage of the {day} price file ({cov['total']} instruments in series EQ/BE/BZ/SM/ST)")
        lines.append(f"   named by at least one source: {cov['named_by_any']}; by source: {cov['by_source']}")
        lines.append(f"   by series [instruments, named]: {by_series}")
        lines.append(f"   NOT named by any source: {len(cov['missing'])}")
        for i in sorted(cov["missing"], key=lambda x: -x.turnover)[:30]:
            lines.append(f"     {i.symbol:<14} series {i.series}  turnover {i.turnover:>16,.0f}  price-file name: {i.name}")
    summary = "\n".join(lines).rstrip()
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-11-18", help="a published trading day, to measure coverage against")
    ap.add_argument("--out", default="samples/names")
    a = ap.parse_args(argv)
    print(run(a.day, a.out))


if __name__ == "__main__":
    main()
