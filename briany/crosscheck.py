"""Do large one-day price moves line up with NSE's corporate-action list?

  python -m briany.crosscheck       (needs REDIS_URL; fetches one year of actions from NSE)
Public data only. Reports counts and examples; it proves nothing by itself.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from briany.marketdata import MIN_JUMP_PRICE
from briany.symbols import is_entitlement


def _pairs(store):
    days = store.all_days()
    return list(zip(days, days[1:]))


def crosscheck(store, actions, threshold=30.0, out=print) -> dict:
    by_symbol = defaultdict(list)
    for a in actions:
        by_symbol[a.symbol].append(a)

    matched, unmatched, kinds = 0, [], Counter()
    for prior, day in _pairs(store):
        rows, prows = store.get_day(day), store.get_day(prior)
        for sym, row in rows.items():
            p = prows.get(sym)
            if not p or p.close < MIN_JUMP_PRICE or is_entitlement(sym):
                continue
            pct = (row.close - p.close) / p.close * 100
            if abs(pct) < threshold:
                continue
            hits = [a for a in by_symbol.get(sym, []) if a.blocking and prior < a.ex_date <= day]
            if hits:
                matched += 1
                kinds[hits[0].kind] += 1
            else:
                unmatched.append((abs(pct), day, sym, p.close, row.close, round(pct, 1)))
    total = matched + len(unmatched)
    out(f"large moves (>= {threshold:g}%): {total}; explained by a blocking corporate action "
        f"with ex-date in that interval: {matched}; unexplained: {len(unmatched)}")
    out("explained, by kind: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
    unmatched.sort(reverse=True)
    out(f"largest unexplained moves (first {min(len(unmatched), 25)}):")
    for _, day, sym, a, b, pct in unmatched[:25]:
        out(f"  {day} {sym}: {a} -> {b} ({pct}%)")

    # the other direction: how visible are bonus/split actions in the price data?
    days = store.all_days()
    visible = Counter()
    first, last = (days[0], days[-1]) if days else (None, None)
    for a in actions:
        if a.kind not in ("bonus", "split") or not days or not (first < a.ex_date <= last):
            continue
        nxt = next((d for d in days if d >= a.ex_date), None)
        idx = days.index(nxt) if nxt else None
        if not idx:
            continue
        p, r = store.get_row(days[idx - 1], a.symbol), store.get_row(nxt, a.symbol)
        if not p or not r or p.close <= 0:
            visible["no price data"] += 1
            continue
        drop = (r.close - p.close) / p.close * 100
        visible["drop >= 30%" if drop <= -30 else "drop 10-30%" if drop <= -10 else "drop < 10% or rise"] += 1
    out("bonus/split actions inside the stored range, by visible price change on the ex-date: "
        + (", ".join(f"{k}: {n}" for k, n in sorted(visible.items())) or "none"))
    return {"total": total, "matched": matched, "unmatched": len(unmatched), "visible": dict(visible),
            "unmatched_rows": unmatched}


def investigate(nse, rows, out=print, limit=120, before=30, after=5):
    """For each unexplained move, show everything NSE lists for that symbol around the date."""
    out(f"investigating {min(len(rows), limit)} unexplained move(s): NSE's own per-symbol feed "
        f"from {before} days before to {after} days after")
    for _, day, sym, _a, _b, pct in rows[:limit]:
        start = (date.fromisoformat(day) - timedelta(days=before)).isoformat()
        end = (date.fromisoformat(day) + timedelta(days=after)).isoformat()
        try:
            acts = nse.corporate_actions_all(symbol=sym, start=start, end=end)[0]
        except Exception as e:  # noqa: BLE001 - report and carry on
            out(f"  {day} {sym} ({pct}%): lookup failed ({type(e).__name__})")
            continue
        listed = "; ".join(f"{a.ex_date} {a.kind}: {a.subject[:60]}" for a in acts) or "nothing listed"
        out(f"  {day} {sym} ({pct}%): {listed}")


def main():
    from briany.ingest import connect
    from briany.nse import NseClient

    _client, store, settings = connect()
    days = store.all_days()
    if not days:
        raise SystemExit("no days stored")
    nse = NseClient(min_interval=settings.nse_min_interval_seconds)
    actions, report = nse.corporate_actions_all(start=days[0], end=days[-1])
    print("per-index report:", report)
    print(f"corporate actions fetched: {len(actions)} "
          f"({sum(1 for a in actions if a.blocking)} blocking) for {days[0]} .. {days[-1]}")
    print("kinds:", dict(Counter(a.kind for a in actions).most_common()))
    res = crosscheck(store, actions)
    print()
    investigate(nse, res["unmatched_rows"])


if __name__ == "__main__":
    main()
