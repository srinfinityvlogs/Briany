"""Daily price storage. MemoryStore for tests/local, RedisStore for production.

Redis layout: one compressed string per day, pxz:<date> (zlib + base64 of lines
"SYMBOL|close|prev_close|volume"), and the sorted set pxz:days (member = date,
score = yyyymmdd) listing fully stored days. The day is added to pxz:days last, so a
half-written day is never visible. (An earlier layout used one hash per day, px:<date>,
which used far too much memory; purge_legacy() removes it.)
"""
from __future__ import annotations

import base64
import zlib

from briany.marketdata import PriceRow

DAYS_KEY = "pxz:days"
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


class MemoryStore:
    def __init__(self):
        self.days: dict[str, dict[str, PriceRow]] = {}

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

    def earliest_day(self):
        res = self.r.zrange(DAYS_KEY, 0, 0)
        return res[0] if res else None
