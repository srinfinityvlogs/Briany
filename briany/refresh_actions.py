"""Keep a compact copy of NSE corporate actions in Redis so answers never call NSE for them.

  python -m briany.ingest --refresh-actions
Replaces the stored copy only if ALL indexes (equities, sme, mf) downloaded and the result
is plausibly large. On any failure the previous copy stays untouched.
"""
from __future__ import annotations

from datetime import date, timedelta

MIN_RECORDS = 500


def refresh_actions(store, nse, today: str, back_days: int = 400, ahead_days: int = 60,
                    min_records: int = MIN_RECORDS, log=print) -> dict:
    start = (date.fromisoformat(today) - timedelta(days=back_days)).isoformat()
    end = (date.fromisoformat(today) + timedelta(days=ahead_days)).isoformat()
    actions, report = nse.corporate_actions_all(start=start, end=end, strict=True)
    if len(actions) < min_records:
        raise RuntimeError(f"only {len(actions)} corporate actions fetched (expected at least {min_records}); "
                           "keeping the previous copy")
    meta = {"updated": today, "start": start, "end": end, "count": len(actions),
            "blocking": sum(1 for a in actions if a.blocking),
            "symbols": len({a.symbol for a in actions}), "report": report}
    store.set_actions(actions, meta)
    log(f"corporate actions refreshed: {meta['count']} records ({meta['blocking']} blocking) for "
        f"{meta['symbols']} symbols, window {start}..{end}; per index {report}")
    return meta
