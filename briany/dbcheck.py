"""Read-only report on the stored price history. Public data only; safe to share."""
from datetime import date, timedelta

from briany.marketdata import PREV_CLOSE_TOLERANCE, daily_move, period_return, period_start

# Reliance closes copied from NSE end-of-day files you downloaded yourself (Nov 2026).
BIG_MOVE_PCT = 30.0   # candidate splits/bonuses and uncapped movers; NOT proof of anything

KNOWN_CLOSES = {"RELIANCE": {"2026-11-03": 1307.90, "2026-11-04": 1311.00,
                             "2026-11-05": 1313.40, "2026-11-06": 1320.00}}


def chain_breaks(store, day, prior):
    """Symbols whose stated previous close differs from the prior stored day's close."""
    rows, prows = store.get_day(day), store.get_day(prior)
    out = []
    for sym, row in rows.items():
        p = prows.get(sym)
        if p and row.prev_close and abs(row.prev_close - p.close) > PREV_CLOSE_TOLERANCE:
            out.append((sym, p.close, row.prev_close))
    return sorted(out)


def run_check(store, out=print) -> bool:
    days = store.all_days()
    if not days:
        out("no days stored")
        return False
    first, last = days[0], days[-1]
    out(f"stored days: {len(days)} ({first} .. {last})")
    have, d, missing = set(days), date.fromisoformat(first), []
    while d <= date.fromisoformat(last):
        if d.weekday() < 5 and d.isoformat() not in have:
            missing.append(d.isoformat())
        d += timedelta(days=1)
    out(f"missing weekdays ({len(missing)}, expected to be market holidays): " + ", ".join(missing[:40]))

    specials = [d for d in days if date.fromisoformat(d).weekday() >= 5]
    out(f"weekend sessions stored ({len(specials)}): " + ", ".join(specials))

    ok = True
    for sym, expected in KNOWN_CLOSES.items():
        parts = []
        for day, want in expected.items():
            row = store.get_row(day, sym)
            good = row is not None and abs(row.close - want) < 0.005
            ok &= good
            parts.append(f"{day} {row.close if row else 'MISSING'} {'OK' if good else 'MISMATCH (expected ' + str(want) + ')'}")
        out(f"known closes {sym}: " + " | ".join(parts))

    tail = days[-5:]
    counts = []
    for prior, day in zip(tail, tail[1:]):
        counts.append(f"{day}: {len(chain_breaks(store, day, prior))}")
    out("symbols whose previous close != prior stored close, latest days: " + ", ".join(counts))
    if len(days) >= 2:
        sample = chain_breaks(store, days[-1], days[-2])[:8]
        out("sample (symbol, prior close, stated previous close): " + "; ".join(f"{s} {a} -> {b}" for s, a, b in sample))

    audit, moves = [], []
    for prior, day in zip(days, days[1:]):
        rows, prows, n = store.get_day(day), store.get_day(prior), 0
        for sym, row in rows.items():
            p = prows.get(sym)
            if not p:
                continue
            if row.prev_close and abs(row.prev_close - p.close) > PREV_CLOSE_TOLERANCE:
                n += 1
            if p.close > 0:
                pct = (row.close - p.close) / p.close * 100
                if abs(pct) >= BIG_MOVE_PCT:
                    moves.append((abs(pct), day, sym, p.close, row.close, round(pct, 1)))
        if n:
            audit.append((day, n))
    out(f"chain audit over {max(len(days) - 1, 0)} day pairs: {len(audit)} day(s) with breaks")
    out("days with breaks (date: symbols): " + ", ".join(f"{d}: {n}" for d, n in audit[:30]))
    moves.sort(reverse=True)
    out(f"large one-day moves (|change| >= {BIG_MOVE_PCT:g}% vs prior stored close): {len(moves)} total; "
        f"largest {min(len(moves), 25)}:")
    for _, day, sym, a, b, pct in moves[:25]:
        out(f"  {day} {sym}: {a} -> {b} ({pct}%)")

    m = daily_move(store, "RELIANCE")
    out(f"RELIANCE daily move: {m.status} {m.date} close {m.close} prev {m.prev_close} pct {m.pct} chain_ok {m.prev_matches_store}")
    for label in ("1m", "1y"):
        r = period_return(store, "RELIANCE", last, period_start(last, label))
        out(f"RELIANCE {label} raw return (no corporate action data yet): {r.status} {r.start_date}->{r.end_date} "
            f"{r.start_close}->{r.end_close} pct {r.pct}")
    out("CHECK " + ("PASSED" if ok else "FAILED"))
    return ok
