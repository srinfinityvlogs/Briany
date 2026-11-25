# SPDX-License-Identifier: Apache-2.0
"""Command line for the daily FII/DII and deal snapshots (see briany/flows.py).

  python -m briany.flows_ingest            fetch NSE's two latest-day feeds and store what is complete
  python -m briany.flows_ingest --check    read-only report on what is stored
  python -m briany.flows_ingest --verify   read-only: exit 1 if any trading day since storage began is missing data
Exit code 1 when a feed could not be fetched or read, or a kind was incomplete (the run then shows red in GitHub Actions).
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="Store the daily FII/DII and large-deal snapshots.")
    ap.add_argument("--check", action="store_true", help="read-only report on what is stored")
    ap.add_argument("--verify", action="store_true", help="read-only: exit 1 if a trading day since storage began is incomplete")
    a = ap.parse_args(argv)
    from briany.flows import flows_report, ingest_flows, verify_flows
    from briany.sessions import special_days_from_env
    from briany.ingest import connect, memory_report
    from briany.nse import NseClient
    client, store, settings = connect()
    mem = memory_report(client, settings.redis_max_memory_mb)
    if mem:
        used, limit = mem
        print(f"redis memory: {used / 1e6:.1f} MB used" + (f" of {limit / 1e6:.1f} MB" if limit else " (no limit reported)"))
    if a.verify:
        lines, ok = verify_flows(store, special_days_from_env())
        print("\n".join(lines))
        sys.exit(0 if ok else 1)
    if a.check:
        print("\n".join(flows_report(store)))
        return
    lines, errors = ingest_flows(NseClient(min_interval=1.0), store)
    print("\n".join(lines))
    print("\n".join(flows_report(store, last=3)))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
