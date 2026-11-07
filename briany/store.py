"""Daily price storage. MemoryStore for tests/local, RedisStore for production.

Redis layout: hash px:<date> (symbol -> "close|prev_close|volume") and the sorted
set px:days (member = date, score = yyyymmdd) listing days that are fully stored.
The day is added to px:days last, so a half-written day is never visible.
"""
from __future__ import annotations

from briany.marketdata import PriceRow

DAYS_KEY = "px:days"


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


class RedisStore:
    def __init__(self, client, chunk: int = 2000):
        self.r = client            # redis.Redis created with decode_responses=True
        self.chunk = chunk

    def put_day(self, day, rows):
        key = f"px:{day}"
        pipe = self.r.pipeline(transaction=False)
        pipe.delete(key)
        items = list(rows.items())
        for i in range(0, len(items), self.chunk):
            pipe.hset(key, mapping={s: encode(r) for s, r in items[i:i + self.chunk]})
        pipe.zadd(DAYS_KEY, {day: _score(day)})
        pipe.execute()

    def has_day(self, day):
        return self.r.zscore(DAYS_KEY, day) is not None

    def get_row(self, day, symbol):
        v = self.r.hget(f"px:{day}", symbol)
        return decode(v) if v else None

    def day_on_or_before(self, day):
        res = self.r.zrevrangebyscore(DAYS_KEY, _score(day), "-inf", start=0, num=1)
        return res[0] if res else None

    def latest_day(self):
        res = self.r.zrevrange(DAYS_KEY, 0, 0)
        return res[0] if res else None

    def earliest_day(self):
        res = self.r.zrange(DAYS_KEY, 0, 0)
        return res[0] if res else None
