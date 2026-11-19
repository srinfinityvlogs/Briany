# SPDX-License-Identifier: Apache-2.0
"""Record real inputs once, replay them offline: the basis of the Step 9 regression tests.

capture_snapshot() runs a list of scenarios through the real Assembler while recording every NSE and news call, then
copies the stored prices, corporate actions and instrument list the scenarios touched. replay_assembler() rebuilds an
Assembler from that file with no network, a fixed clock and a MemoryStore, so the same scenarios must give the same
packets. Public data only; no secret is ever read here.
"""
from __future__ import annotations

import dataclasses
import gzip
import json
from datetime import datetime

from briany.evidence import IST, Assembler
from briany.marketdata import PriceRow
from briany.news import NewsItem, NewsResult
from briany.nse import NseError
from briany.nse_parsers import Announcement, BoardMeeting, CorpAction, Deal, EventItem, FiiDii
from briany.store import MemoryStore, StoredAction, pack_day, pack_instruments, unpack_day, unpack_instruments
from briany.symbols import Resolver

VERSION = 1
PERIODS = ("1w", "1m", "3m", "6m", "1y", "ytd")
_CLASSES = {c.__name__: c for c in (Announcement, BoardMeeting, CorpAction, Deal, EventItem, FiiDii, NewsItem,
                                    NewsResult, StoredAction)}


# ------------------------------------------------------------------ JSON form of dataclasses and tuples
def encode(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {"__dc__": type(obj).__name__, "f": {f.name: encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}}
    if isinstance(obj, tuple):
        return {"__tuple__": [encode(x) for x in obj]}
    if isinstance(obj, list):
        return [encode(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): encode(v) for k, v in obj.items()}
    return obj


def decode(obj):
    if isinstance(obj, list):
        return [decode(x) for x in obj]
    if isinstance(obj, dict):
        if "__dc__" in obj:
            return _CLASSES[obj["__dc__"]](**{k: decode(v) for k, v in obj["f"].items()})
        if "__tuple__" in obj:
            return tuple(decode(x) for x in obj["__tuple__"])
        return {k: decode(v) for k, v in obj.items()}
    return obj


def jsonable(packet) -> dict:
    """A packet as plain JSON data (tuples become lists), the form stored and compared."""
    return json.loads(json.dumps(packet.to_dict(), ensure_ascii=False))


def call_key(name, args, kwargs) -> str:
    return json.dumps([name, list(args), sorted(kwargs.items())], default=str, sort_keys=True)


# ------------------------------------------------------------------ recording and replaying a client
class Recorder:
    """Wraps a real client; every call and its result (or NseError) is kept."""

    def __init__(self, target):
        self._target, self.calls = target, {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        fn = getattr(self._target, name)

        def wrapped(*args, **kwargs):
            key = call_key(name, args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except NseError as e:
                self.calls[key] = {"error": ["NseError", str(e)]}
                raise
            self.calls[key] = {"ok": encode(result)}
            return result
        return wrapped


class Replay:
    """Answers exactly the recorded calls. An unrecorded call means the code now asks for something different."""

    def __init__(self, calls: dict):
        self._calls = calls

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def replayed(*args, **kwargs):
            key = call_key(name, args, kwargs)
            rec = self._calls.get(key)
            if rec is None:
                raise KeyError(f"unrecorded call: {key}")
            if "error" in rec:
                raise NseError(rec["error"][1])
            return decode(rec["ok"])
        return replayed


# ------------------------------------------------------------------ scenarios
def build_calls(queries, days, periods=PERIODS) -> list:
    """The Phase 1 scenarios: why it moved on each day, returns to the latest close, and recent events."""
    calls = []
    for q in queries:
        calls += [{"id": f"why:{q}:{d}", "fn": "why_moved", "args": {"query": q, "on": d}} for d in days]
        calls += [{"id": f"return:{q}:{p}", "fn": "period_return", "args": {"query": q, "label": p}} for p in periods]
        calls.append({"id": f"events:{q}", "fn": "events", "args": {"query": q}})
    return calls


def run_call(asm, call):
    return getattr(asm, call["fn"])(**call["args"])


# ------------------------------------------------------------------ capture
def capture_snapshot(store, instruments, nse, news, now, calls) -> dict:
    resolver = Resolver(instruments)
    rec_nse, rec_news = Recorder(nse), Recorder(news)
    asm = Assembler(store, rec_nse, rec_news, resolver, now=lambda: now)
    results = [{"id": c["id"], "call": c, "packet": jsonable(run_call(asm, c))} for c in calls]
    symbols = sorted({r["packet"]["symbol"] for r in results if r["packet"]["symbol"]})
    scenario_days = sorted({c["args"]["on"] for c in calls if c["args"].get("on")})
    days = store.all_days()
    prices = {s: {} for s in symbols}
    full_days = {}
    for d in days:
        rows = store.get_day(d)
        for s in symbols:
            r = rows.get(s)
            if r is not None:
                prices[s][d] = [r.close, r.prev_close, r.volume]
        if d in scenario_days:
            full_days[d] = pack_day(rows)                         # the whole market that day, for the breadth context
    return {
        "version": VERSION,
        "captured_at": now.isoformat(timespec="seconds"),
        "latest_day": store.latest_day(),
        "days": days,
        "symbols": symbols,
        "instruments": pack_instruments(instruments),
        "prices": prices,
        "full_days": full_days,
        "actions": {s: encode(store.get_actions(s)) for s in symbols},
        "actions_meta": store.get_actions_meta(),
        "jumps": {s: [list(j) for j in store.get_jumps(s)] for s in symbols},
        "nse_calls": rec_nse.calls,
        "news_calls": rec_news.calls,
        "results": results,
    }


def save_snapshot(snap: dict, path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))


def load_snapshot(path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ replay
def replay_store(snap: dict) -> MemoryStore:
    store = MemoryStore()
    full = {d: unpack_day(blob) for d, blob in snap["full_days"].items()}
    for d in snap["days"]:
        rows = dict(full.get(d, {}))
        for s, per_day in snap["prices"].items():
            if d in per_day:
                close, prev, vol = per_day[d]
                rows[s] = PriceRow(close, prev, vol)
        store.put_day(d, rows)                                    # a day with no row for any captured stock stays listed
    store.instruments = ("snapshot", unpack_instruments(snap["instruments"]))
    store.actions = {s: decode(v) for s, v in snap["actions"].items()}
    store.actions_meta = dict(snap["actions_meta"]) if snap["actions_meta"] else None
    store.jumps = {s: [tuple(j) for j in v] for s, v in snap["jumps"].items()}
    return store


def replay_assembler(snap: dict):
    """(Assembler, now): offline, with the clock the capture ran at."""
    now = datetime.fromisoformat(snap["captured_at"]).astimezone(IST)
    store = replay_store(snap)
    _day, instruments = store.get_instruments()
    asm = Assembler(store, Replay(snap["nse_calls"]), Replay(snap["news_calls"]), Resolver(instruments),
                    now=lambda: now)
    return asm, now


# ------------------------------------------------------------------ a readable summary to check against a broker
def summarize(results) -> str:
    lines = []
    for r in results:
        p = r["packet"]
        facts = "; ".join(f"{f['label']}: {f['value']}" for f in p["facts"])
        ctx = "; ".join(f"{f['label']}: {f['value']}" for f in p["context"])
        gaps = ",".join(g["code"] for g in p["gaps"]) or "-"
        lines.append(f"{r['id']}  [{p['kind']}/{p['status']}] {p['symbol']} | {facts or '-'}"
                     + (f" | context: {ctx}" if ctx else "") + f" | factors: {len(p['factors'])} | gaps: {gaps}")
    return "\n".join(lines)


# ------------------------------------------------------------------ keeping the golden packets current
def rebaseline(snap: dict) -> list:
    """Re-run every scenario offline and store the packets the CURRENT code gives; returns the ids that changed.
    Run it only after reading the change: it makes the golden test pass by accepting the new output."""
    asm, _now = replay_assembler(snap)
    changed = []
    for r in snap["results"]:
        packet = jsonable(run_call(asm, r["call"]))
        if packet != r["packet"]:
            changed.append(r["id"])
            r["packet"] = packet
    return changed


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python -m briany.snapshot tests/fixtures/phase1/phase1_scenarios.json.gz")
    data = load_snapshot(sys.argv[1])
    ids = rebaseline(data)
    if ids:
        save_snapshot(data, sys.argv[1])
    print(f"{len(ids)} packet(s) changed and accepted:" + "".join(f"\n  {i}" for i in ids) if ids else "no packet changed")
