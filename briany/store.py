"""Daily price storage. MemoryStore for tests/local, RedisStore for production.

Redis layout: one compressed string per day, pxz:<date> (zlib + base64 of lines
"SYMBOL|close|prev_close|volume"), and the sorted set pxz:days (member = date,
score = yyyymmdd) listing fully stored days. The day is added to pxz:days last, so a
half-written day is never visible. (An earlier layout used one hash per day, px:<date>,
which used far too much memory; purge_legacy() removes it.)
"""
from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass

from briany.marketdata import PriceRow

DAYS_KEY = "pxz:days"
JUMPS_KEY = "pxz:jumps"
ACTIONS_KEY = "pxz:actions"
ACTIONS_META_KEY = "pxz:actions:meta"
DAY_PREFIX = "pxz:"
LEGACY_PATTERN = "px:*"


def encode(row: PriceRow) -> str:
    return f"{row.close!r}|{'' if row.prev_close is None else repr(row.prev_close)}|" \
           f"{'' if row.volume is None else row.volume}"


def decode(text: str) -> PriceRow:
    close, prev, vol = text.split("|")
    return PriceRow(float(close), float(prev) if prev else None, int(vol) if vol else None)


def _score(day: str) -> int:
    return int(day.replace("-", ""))


@dataclass(frozen=True)
class StoredAction:
    ex_date: str
    kind: str
    blocking: bool
    subject: str
    amount: float | None = None      # dividends only


def pack_actions(actions) -> dict:
    """CorpAction-like objects -> {symbol: compact JSON}. Subjects are cut to 100 chars."""
    by: dict[str, list] = {}
    for a in actions:
        amount = (getattr(a, "detail", None) or {}).get("amount") if a.kind == "dividend" else None
        by.setdefault(a.symbol, []).append({"d": a.ex_date, "k": a.kind, "b": 1 if a.blocking else 0,
                                            "s": a.subject[:100], "a": amount})
    return {sym: json.dumps(sorted(v, key=lambda x: (x["d"], x["k"])), separators=(",", ":"))
            for sym, v in by.items()}


def unpack_actions(text: str) -> list:
    return [StoredAction(x["d"], x["k"], bool(x["b"]), x["s"], x.get("a")) for x in json.loads(text)]


def _merge_jumps(existing, new):
    """existing: [[date, pct], ...]; new: [(date, pct), ...] -> sorted, one entry per date."""
    by_date = {d: p for d, p in existing}
    by_date.update(dict(new))
    return [[d, by_date[d]] for d in sorted(by_date)]


class MemoryStore:
    def __init__(self):
        self.days: dict[str, dict[str, PriceRow]] = {}
        self.jumps: dict[str, list] = {}
        self.actions: dict[str, list] = {}
        self.actions_meta = None

    def set_actions(self, actions, meta):
        self.actions = {sym: unpack_actions(v) for sym, v in pack_actions(actions).items()}
        self.actions_meta = dict(meta)

    def get_actions(self, symbol):
        return list(self.actions.get(symbol, []))

    def get_actions_meta(self):
        return dict(self.actions_meta) if self.actions_meta else None

    def add_jumps(self, entries):
        grouped: dict[str, list] = {}
        for sym, day, pct in entries:
            grouped.setdefault(sym, []).append((day, pct))
        for sym, items in grouped.items():
            self.jumps[sym] = _merge_jumps(self.jumps.get(sym, []), items)

    def get_jumps(self, symbol):
        return [(d, p) for d, p in self.jumps.get(symbol, [])]

    def all_jumps(self):
        return {s: [(d, p) for d, p in v] for s, v in self.jumps.items()}

    def clear_jumps(self):
        self.jumps = {}

    def put_day(self, day, rows):
        self.days[day] = dict(rows)

    def has_day(self, day):
        return day in self.days

    def get_row(self, day, symbol):
        return self.days.get(day, {}).get(symbol)

    def day_on_or_before(self, day):
        ok = [d for d in self.days if d <= day]
        return max(ok) if ok else None

    def latest_day(self):
        return max(self.days) if self.days else None

    def all_days(self):
        return sorted(self.days)

    def prune_before(self, cutoff, max_days=40):
        """Delete stored days strictly before `cutoff` (oldest first, at most max_days)."""
        old = sorted(d for d in self.days if d < cutoff)[:max_days]
        for d in old:
            del self.days[d]
        return old

    def get_day(self, day):
        return dict(self.days.get(day, {}))

    def earliest_day(self):
        return min(self.days) if self.days else None


def pack_day(rows) -> str:
    lines = [f"{sym}|{encode(row)}" for sym, row in rows.items()]
    return base64.b64encode(zlib.compress("\n".join(lines).encode("utf-8"), 9)).decode("ascii")


def unpack_day(blob: str) -> dict:
    raw = zlib.decompress(base64.b64decode(blob)).decode("utf-8")
    out = {}
    for line in raw.split("\n"):
        sym, rest = line.split("|", 1)
        out[sym] = decode(rest)
    return out


def purge_legacy(client, batch: int = 100) -> int:
    """Delete the old per-day hashes (px:*) and index. Returns the number of keys removed."""
    removed, pending = 0, []
    for key in client.scan_iter(match=LEGACY_PATTERN, count=500):
        pending.append(key)
        if len(pending) >= batch:
            removed += client.delete(*pending)
            pending = []
    if pending:
        removed += client.delete(*pending)
    return removed


class RedisStore:
    CACHE_DAYS = 32

    def __init__(self, client):
        self.r = client            # redis.Redis created with decode_responses=True
        self._cache: dict = {}

    def put_day(self, day, rows):
        pipe = self.r.pipeline(transaction=False)
        pipe.set(f"{DAY_PREFIX}{day}", pack_day(rows))
        pipe.zadd(DAYS_KEY, {day: _score(day)})      # last: only complete days become visible
        pipe.execute()
        self._cache.pop(day, None)

    def has_day(self, day):
        return self.r.zscore(DAYS_KEY, day) is not None

    def set_actions(self, actions, meta):
        """Replace the stored copy atomically: build under a temp key, then RENAME over it."""
        packed, tmp = pack_actions(actions), ACTIONS_KEY + ":new"
        items = list(packed.items())
        pipe = self.r.pipeline(transaction=False)
        pipe.delete(tmp)
        for i in range(0, len(items), 500):
            pipe.hset(tmp, mapping=dict(items[i:i + 500]))
        if items:
            pipe.rename(tmp, ACTIONS_KEY)
        else:
            pipe.delete(ACTIONS_KEY)
        pipe.set(ACTIONS_META_KEY, json.dumps(meta))
        pipe.execute()

    def get_actions(self, symbol):
        v = self.r.hget(ACTIONS_KEY, symbol)
        return unpack_actions(v) if v else []

    def get_actions_meta(self):
        v = self.r.get(ACTIONS_META_KEY)
        return json.loads(v) if v else None

    def add_jumps(self, entries):
        grouped: dict[str, list] = {}
        for sym, day, pct in entries:
            grouped.setdefault(sym, []).append((day, pct))
        if not grouped:
            return
        syms = list(grouped)
        current = self.r.hmget(JUMPS_KEY, syms)
        mapping = {sym: json.dumps(_merge_jumps(json.loads(cur) if cur else [], grouped[sym]))
                   for sym, cur in zip(syms, current)}
        self.r.hset(JUMPS_KEY, mapping=mapping)

    def get_jumps(self, symbol):
        v = self.r.hget(JUMPS_KEY, symbol)
        return [(d, p) for d, p in json.loads(v)] if v else []

    def all_jumps(self):
        return {s: [(d, p) for d, p in json.loads(v)] for s, v in self.r.hgetall(JUMPS_KEY).items()}

    def clear_jumps(self):
        self.r.delete(JUMPS_KEY)

    def _load(self, day):
        if day in self._cache:
            return self._cache[day]
        blob = self.r.get(f"{DAY_PREFIX}{day}")
        if not blob:
            return {}
        rows = unpack_day(blob)
        if len(self._cache) >= self.CACHE_DAYS:
            self._cache.pop(next(iter(self._cache)))
        self._cache[day] = rows
        return rows

    def get_row(self, day, symbol):
        return self._load(day).get(symbol)

    def day_on_or_before(self, day):
        res = self.r.zrevrangebyscore(DAYS_KEY, _score(day), "-inf", start=0, num=1)
        return res[0] if res else None

    def latest_day(self):
        res = self.r.zrevrange(DAYS_KEY, 0, 0)
        return res[0] if res else None

    def all_days(self):
        return list(self.r.zrange(DAYS_KEY, 0, -1))

    def prune_before(self, cutoff, max_days=40):
        """Delete stored days strictly before `cutoff` (oldest first, at most max_days).
        The index entry goes first, so no day is ever listed without its data."""
        old = self.r.zrangebyscore(DAYS_KEY, "-inf", f"({_score(cutoff)}", start=0, num=max_days)
        if not old:
            return []
        pipe = self.r.pipeline(transaction=False)
        pipe.zrem(DAYS_KEY, *old)
        pipe.delete(*[f"{DAY_PREFIX}{d}" for d in old])
        pipe.execute()
        for d in old:
            self._cache.pop(d, None)
        return list(old)

    def get_day(self, day):
        return dict(self._load(day))

    def earliest_day(self):
        res = self.r.zrange(DAYS_KEY, 0, 0)
        return res[0] if res else None
