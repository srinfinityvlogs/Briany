# SPDX-License-Identifier: Apache-2.0
"""Build one evidence packet from real stored data and print it as JSON (public data only).

  python -m briany.assemble_cli --mode why --query "reliance"
  python -m briany.assemble_cli --mode return --query tcs --period 1m
  python -m briany.assemble_cli --mode events --query infosys
  python -m briany.assemble_cli --mode index --query "nifty bank" --on 2026-11-03
  python -m briany.assemble_cli --mode flows --query 1w
  python -m briany.assemble_cli --mode deals --query all --period bulk
"""
from __future__ import annotations

import argparse
import json
import sys

from briany.evidence import Assembler
from briany.indexfile import IndexClient
from briany.news import NewsClient
from briany.nse import NseClient
from briany.symbols import Resolver


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("why", "return", "events", "index", "flows", "deals"), required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--on", help="date YYYY-MM-DD (why, index, flows and deals modes)")
    ap.add_argument("--period", help="1w, 1m, 3m, 6m, 1y, ytd (return mode); bulk, block or short (deals mode)")
    a = ap.parse_args(argv)

    from briany.ingest import connect
    _client, store, settings = connect()
    day, instruments = store.get_instruments()
    if not instruments:
        sys.exit("no instrument list stored; run the ingest workflow with refresh_instruments = yes")
    asm = Assembler(store, NseClient(min_interval=settings.nse_min_interval_seconds), NewsClient(),
                    Resolver(instruments), index=IndexClient())
    if a.mode == "why":
        packet = asm.why_moved(a.query, on=a.on)
    elif a.mode == "flows":                       # --query: a period (1w..ytd) for totals, anything else (day) for one day
        packet = asm.fii_dii(on=a.on or None, label=a.query if a.query in ("1w", "1m", "3m", "6m", "1y", "ytd") else None)
    elif a.mode == "deals":                       # --query: a company, or 'all' for the market; --period: bulk, block or short
        packet = asm.large_deals(query=None if a.query.lower() in ("all", "market", "") else a.query,
                                 kind=a.period if a.period in ("bulk", "block", "short") else None, on=a.on or None)
    elif a.mode == "index":
        packet = asm.index_close(a.query, on=a.on)
    elif a.mode == "return":
        packet = asm.period_return(a.query, label=a.period or "1m")
    else:
        packet = asm.events(a.query)
    print(f"instrument list from the {day} file")
    print(json.dumps(packet.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
