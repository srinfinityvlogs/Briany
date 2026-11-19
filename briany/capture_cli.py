"""Record the Phase 1 scenarios from real data (public data only; no secret is printed).

  python -m briany.capture_cli --days 2026-11-03,2026-11-02,2026-10-30 --out snapshot
Writes snapshot/phase1_scenarios.json.gz (the recording) and snapshot/summary.txt (what to check against a broker).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from briany.evidence import IST
from briany.news import NewsClient
from briany.nse import NseClient
from briany.snapshot import build_calls, capture_snapshot, save_snapshot, summarize

QUERIES = ("reliance", "tcs", "infosys", "tempsens", "krn", "goldbees")      # the Phase 1 stocks and ETF


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="2026-11-03,2026-11-02,2026-10-30")
    ap.add_argument("--queries", default=",".join(QUERIES))
    ap.add_argument("--out", default="snapshot")
    a = ap.parse_args(argv)
    days = [d.strip() for d in a.days.split(",") if d.strip()]
    queries = [q.strip() for q in a.queries.split(",") if q.strip()]
    if not days or not queries:
        sys.exit("give at least one day and one query")

    from briany.ingest import connect
    _client, store, settings = connect()
    _day, instruments = store.get_instruments()
    if not instruments:
        sys.exit("no instrument list stored; run ingest with refresh_instruments = yes")
    now = datetime.now(IST).replace(microsecond=0)
    snap = capture_snapshot(store, instruments, NseClient(min_interval=settings.nse_min_interval_seconds), NewsClient(),
                            now, build_calls(queries, days))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    save_snapshot(snap, out / "phase1_scenarios.json.gz")
    text = summarize(snap["results"])
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    failed = [r["id"] for r in snap["results"] if any(g["code"].endswith("_unavailable") for g in r["packet"]["gaps"])]
    print(f"\n{len(snap['results'])} scenarios; latest stored day {snap['latest_day']}; symbols {', '.join(snap['symbols'])}")
    print("scenarios with a source that failed to answer (re-run if any): " + (", ".join(failed) or "none"))


if __name__ == "__main__":
    main()
