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

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from briany import nse_parsers as P
from briany.nse import NseError
from briany.sessions import is_special_session

IST = ZoneInfo("Asia/Kolkata")
KEEP_DAYS = 400                      # calendar days kept, measured from the newest stored day
CATEGORIES = ("DII", "FII/FPI")      # both must be present for a day's flows to be stored
KINDS = (("bulk", "BULK_DEALS"), ("block", "BLOCK_DEALS"), ("short", "SHORT_DEALS"))
RETRY_WAITS = (30, 90)               # seconds to wait before trying again after NSE refuses (403) or sends a page, not JSON
REQUIRED = ("fii", "bulk", "block")  # what every trading day since storage began must have (short rows run a day behind)
# Days that were lost for good: NSE keeps only the latest day, so a day the ingest could not store cannot be added later.
# Listed here so that verify stays green for them and goes red only for a NEW missing day. More can be added without a code
# change through the KNOWN_FLOW_GAPS repository variable (comma-separated dates).
KNOWN_GAPS = frozenset({"2026-11-24"})   # NSE answered the GitHub runners with HTTP 403 all that day


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


def _fetch(nse, path, sleep, waits):
    """(data, number of retries used). NSE sometimes answers a GitHub runner with HTTP 403 or a block page for a while; this
    job is not waiting for a person, so it waits and asks again. Any other error (5xx, network) is already retried inside
    NseClient and is raised here at once."""
    for attempt in range(len(waits) + 1):
        try:
            return nse.get_json(path), attempt
        except NseError as e:
            blocked = "HTTP 403" in str(e) or "non-JSON" in str(e)
            if not blocked or attempt == len(waits):
                raise
            sleep(waits[attempt])


def ingest_flows(nse, store, now=None, sleep=time.sleep, waits=RETRY_WAITS):
    """Fetch both feeds, store what is complete. Returns (report lines, error count). A feed that cannot be fetched or read
    does not stop the other; a feed that still shows the previous day (not yet published) is simply stored again, unchanged."""
    now = now or datetime.now(IST)
    lines, errors, fii_raw, deals_raw = [], 0, None, None
    for label, path in (("FII/DII", "fiidiiTradeReact"), ("large deals", "snapshot-capital-market-largedeal")):
        try:
            raw, retries = _fetch(nse, path, sleep, waits)
        except NseError as e:
            lines.append(f"ERROR {label}: {e}" + (f" (still refused after {len(waits)} retries)" if "HTTP 403" in str(e) and waits else ""))
            errors += 1
            continue
        if retries:
            lines.append(f"note {label}: NSE refused at first, then answered after {retries} retr{'y' if retries == 1 else 'ies'}")
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


def verify_flows(store, special=frozenset(), known_gaps=KNOWN_GAPS):
    """(report lines, ok). Every weekday trading day since the first stored FII/DII day must have FII/DII, bulk and block
    records: a missing one cannot be recovered (NSE keeps only the latest day), so it is reported with the kinds missing.
    Weekend sessions and days in `special` are skipped (their flows are not assumed). Short rows are not required: they run a
    day behind and are not published for every day. A day in `known_gaps` that is missing is reported as a note and not counted,
    so a loss that can never be repaired does not keep the check red; a day in `known_gaps` that is complete says nothing."""
    flow_days = store.flow_days()
    with_fii = [d for d in flow_days if (store.get_flow(d) or {}).get("fii")]
    if not with_fii:
        return ["PROBLEM no FII/DII record is stored at all"], False
    first = with_fii[0]
    expected = [d for d in store.all_days() if d >= first and not is_special_session(d, special)]
    missing, skipped = [], []
    for d in expected:
        rec = store.get_flow(d) or {}
        # bulk and block may legitimately be stored as an empty list (NSE declared none); FII/DII never is
        gone = [k for k in REQUIRED if k not in rec or (k == "fii" and not rec[k])]
        if gone and d in known_gaps:
            skipped.append((d, gone))
        elif gone:
            missing.append((d, gone))
    lines = [f"checked {len(expected)} trading day(s) from {first} to {expected[-1] if expected else first}: "
             f"FII/DII, bulk and block expected on each"]
    for d, gone in skipped:
        lines.append(f"note {d}: known gap, not counted (missing {', '.join(gone)}; NSE keeps only the latest day, so it cannot be added)")
    if not missing:
        lines.append("ok   every trading day since storage began is complete"
                     + (", apart from the known gap above" if len(skipped) == 1 else ", apart from the known gaps above" if skipped else ""))
        return lines, True
    for d, gone in missing:
        lines.append(f"PROBLEM {d}: missing {', '.join(gone)}")
    lines.append("A day that is missing cannot be rebuilt once NSE shows a newer day. If the newest day is among them, run "
                 "the ingest-flows workflow now.")
    return lines, False


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
