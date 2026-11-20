# SPDX-License-Identifier: Apache-2.0
"""Store NSE's full company names in Redis (run daily by the refresh-names workflow).

  python -m briany.refresh_names
Exits 1 on any problem and leaves the stored names as they were. Never prints the Redis URL.
"""
from __future__ import annotations

import sys
from datetime import datetime

from briany.evidence import IST
from briany.names import NamesError, refresh_names

SHOW = ("RELIANCE", "TCS", "INFY", "TEMPSENS", "KRN")


def main(argv=None, connect=None, fetch=None):
    if connect is None:
        from briany.ingest import connect
    _client, store, _settings = connect()
    kwargs = {"fetch": fetch} if fetch else {}
    today = datetime.now(IST).date().isoformat()
    try:
        counts = refresh_names(store, today, **kwargs)
    except NamesError as e:
        print(f"Names not updated: {e}")
        sys.exit(1)
    print(f"Stored {counts['names']} names ({counts['equity']} equity-list, {counts['sme']} SME-list; "
          f"{counts['previous']} were stored before).")
    _day, names = store.get_names()
    for sym in SHOW:
        print(f"  {sym}: {names.get(sym, '(none)')}")


if __name__ == "__main__":
    main()
