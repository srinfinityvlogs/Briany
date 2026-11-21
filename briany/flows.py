# SPDX-License-Identifier: Apache-2.0
"""Daily snapshots of FII/DII flows and bulk/block/short deals, stored so that 'last week' can be answered later.

Why store: NSE's two feeds give the LATEST DAY ONLY (verified 20 Nov 2026), and no history endpoint is usable (the date-range
deal endpoint returns at most 70 rows, a busy day has 100+). A day that is not stored is lost for good.

  python -m briany.flows_ingest            # fetch both feeds, store what is complete, prune old days
  python -m briany.flows_ingest --check    # read-only report on what is stored

Verified on real files of 20 Nov 2026: fiidiiTradeReact = 2 rows (DII and FII/FPI), values in Rs crore; the large-deal
snapshot declares its counts (BULK_DEALS 108, SHORT_DEALS 84, BLOCK_DEALS 5) next to lists that hold exactly that many rows;
bulk and block rows carry the trading date (20 Nov), short-selling rows are dated one day EARLIER (19 Nov). So every kind is
stored under its own rows' date, and a kind is stored only when its listed rows equal NSE's declared count.

Record per day (JSON, then zlib + base64; about 3 KB compressed for a real day):
  fii    [[category, buy, sell, net], ...]                         Rs crore
  bulk   [[symbol, client, side, qty, price, remarks], ...]        block: same layout
  short  [[symbol, qty], ...]
  declared {kind: count NSE declared}, stored: ISO time of the last write
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from briany import nse_parsers as P
from briany.nse import NseError

IST = ZoneInfo("Asia/Kolkata")
KEEP_DAYS = 400                      # calendar days kept, measured from the newest stored day
CATEGORIES = ("DII", "FII/FPI")      # both must be present for a day's flows to be stored
KINDS = (("bulk", "BULK_DEALS"), ("block", "BLOCK_DEALS"), ("short", "SHORT_DEALS"))


def build_updates(fii_raw, deals_raw):
    """({day: {field: value}}, problems, notes). Pure: no network, no store. Anything incomplete is left out and named."""
    updates, problems, notes = {}, [], []
    if fii_raw is not None:
        try:
            rows = P.parse_fii_dii(fii_raw)
        except P.ShapeError as e:
            rows = []
            problems.append(f"FII/DII: unexpected response ({e})")
        by_day = {}
        for r in rows:
            by_day.setdefault(r.date, []).append(r)
        for day, group in sorted(by_day.items()):
            cats = {r.category for r in group}
            if all(c in cats for c in CATEGORIES):
                updates.setdefault(day, {})["fii"] = [[r.category, r.buy, r.sell, r.net] for r in sorted(group, key=lambda r: r.category)]
                notes.append(f"FII/DII {day}: {len(group)} rows")
            else:
                problems.append(f"FII/DII {day}: categories {sorted(cats)} are not both of {list(CATEGORIES)}; not stored")
        if not rows and not problems:
            problems.append("FII/DII: no rows in the response")
    if deals_raw is not None:
        try:
            deals = P.parse_large_deals(deals_raw)
            as_on = P.parse_date(deals_raw.get("as_on_date"))
        except P.ShapeError as e:
            deals, as_on = [], None
            problems.append(f"large deals: unexpected response ({e})")
        else:
            for kind, key in KINDS:
                declared_raw = str(deals_raw.get(key, "")).strip()
                if not declared_raw.isdigit():
                    problems.append(f"large deals {kind}: no declared count ({key}={declared_raw!r}); not stored")
                    continue
                declared = int(declared_raw)
                listed = [d for d in deals if d.kind == kind]
                if len(listed) != declared:
                    problems.append(f"large deals {kind}: NSE declares {declared} but {len(listed)} usable rows are listed; not stored")
                    continue
                days = sorted({d.date for d in listed})
                if len(days) > 1:
                    problems.append(f"large deals {kind}: rows span several days {days}; not stored")
                    continue
                if not days:
                    if kind == "short" or not as_on:             # an empty short list has no date to file it under
                        notes.append(f"large deals {kind}: none declared; nothing to store")
                        continue
                    days = [as_on]
                day = days[0]
                if kind == "short":
                    rows = [[d.symbol, d.quantity] for d in listed]
                else:
                    rows = [[d.symbol, d.client, d.side, d.quantity, d.price, d.remarks] for d in listed]
                updates.setdefault(day, {})[kind] = rows
                updates[day].setdefault("declared", {})[kind] = declared
                notes.append(f"large deals {kind} {day}: {len(rows)} rows (declared {declared})")
    return updates, problems, notes


def apply_updates(store, updates, now: datetime):
    """Merge into each day's record. A day is rewritten only when something in it actually changed."""
    changed = []
    for day, fields in sorted(updates.items()):
        rec = store.get_flow(day) or {}
        before = {k: v for k, v in rec.items() if k != "stored"}
        for k, v in fields.items():
            if k == "declared":
                rec.setdefault("declared", {}).update(v)
            else:
                rec[k] = v
        if {k: v for k, v in rec.items() if k != "stored"} != before:
            rec["stored"] = now.isoformat(timespec="seconds")
            store.put_flow(day, rec)
            changed.append(day)
    return changed


def prune(store, keep_days: int = KEEP_DAYS):
    days = store.flow_days()
    if not days:
        return []
    cutoff = (datetime.fromisoformat(days[-1]) - timedelta(days=keep_days)).date().isoformat()
    return store.prune_flows(cutoff)


def ingest_flows(nse, store, now=None):
    """Fetch both feeds, store what is complete. Returns (report lines, error count). A feed that cannot be fetched or read
    does not stop the other; a feed that still shows the previous day (not yet published) is simply stored again, unchanged."""
    now = now or datetime.now(IST)
    lines, errors, fii_raw, deals_raw = [], 0, None, None
    for label, path in (("FII/DII", "fiidiiTradeReact"), ("large deals", "snapshot-capital-market-largedeal")):
        try:
            raw = nse.get_json(path)
        except NseError as e:
            lines.append(f"ERROR {label}: {e}")
            errors += 1
            continue
        if path == "fiidiiTradeReact":
            fii_raw = raw
        else:
            deals_raw = raw
    updates, problems, notes = build_updates(fii_raw, deals_raw)
    changed = apply_updates(store, updates, now)
    lines += [f"ok   {n}" for n in notes]
    lines += [f"WARN {p}" for p in problems]
    errors += len(problems)
    lines.append("rewritten: " + (", ".join(changed) if changed else "nothing (already stored, unchanged)"))
    pruned = prune(store)
    if pruned:
        lines.append(f"pruned {len(pruned)} old day(s): {pruned[0]} .. {pruned[-1]}")
    return lines, errors


def flows_report(store, last: int = 8):
    days = store.flow_days()
    if not days:
        return ["no FII/DII or deal records stored yet"]
    lines = [f"{len(days)} day record(s) stored, {days[0]} .. {days[-1]}"]
    total = 0
    for d in days:
        total += store.flow_bytes(d)
    lines.append(f"stored size: {total / 1000:.1f} KB in total (compressed), about {total / len(days) / 1000:.1f} KB a day")
    for d in days[-last:]:
        rec = store.get_flow(d) or {}
        parts = [f"fii {len(rec['fii'])} rows" if "fii" in rec else "fii -"]
        for kind in ("bulk", "block", "short"):
            parts.append(f"{kind} {len(rec[kind])}" if kind in rec else f"{kind} -")
        lines.append(f"  {d}: " + ", ".join(parts) + f"  ({store.flow_bytes(d) / 1000:.1f} KB)")
    return lines
