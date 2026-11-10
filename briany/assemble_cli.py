"""Build one evidence packet from real stored data and print it as JSON (public data only).

  python -m briany.assemble_cli --mode why --query "reliance"
  python -m briany.assemble_cli --mode return --query tcs --period 1m
  python -m briany.assemble_cli --mode events --query infosys
"""
from __future__ import annotations

import argparse
import json
import sys

from briany.evidence import Assembler
from briany.news import NewsClient
from briany.nse import NseClient
from briany.symbols import Resolver


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("why", "return", "events"), required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--on", help="date YYYY-MM-DD (why mode)")
    ap.add_argument("--period", help="1w, 1m, 3m, 6m, 1y, ytd (return mode)")
    a = ap.parse_args(argv)

    from briany.ingest import connect
    _client, store, settings = connect()
    day, instruments = store.get_instruments()
    if not instruments:
        sys.exit("no instrument list stored; run the ingest workflow with refresh_instruments = yes")
    asm = Assembler(store, NseClient(min_interval=settings.nse_min_interval_seconds), NewsClient(),
                    Resolver(instruments))
    if a.mode == "why":
        packet = asm.why_moved(a.query, on=a.on)
    elif a.mode == "return":
        packet = asm.period_return(a.query, label=a.period or "1m")
    else:
        packet = asm.events(a.query)
    print(f"instrument list from the {day} file")
    print(json.dumps(packet.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
