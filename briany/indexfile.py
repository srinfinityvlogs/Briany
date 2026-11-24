# SPDX-License-Identifier: Apache-2.0
"""NSE's end-of-day index file: parser and on-demand client. Stdlib only.

  https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv

Facts verified on real files (20 Nov 2026, days 3 Nov, 30 Oct and 18 Nov): 13 columns, DD-MM-YYYY dates, 167 to 169 rows,
numbers such as '-.63' and '.89', '-' where a figure does not apply, older days exist. A day with no file (10 Nov holiday)
does NOT answer 404: it answers HTTP 200 with an HTML 'Service Temporarily Unavailable' page. So the status code proves
nothing here: the body is checked, and anything that is not a complete index CSV for exactly the requested day raises
IndexFileError (a subclass of NseError, so every caller that already handles NSE failures handles this one).
Nothing is stored for days: a parsed day is cached in Redis (an hour for today and yesterday, a week for older days) and fetched again when needed.
"""
from __future__ import annotations

import csv
import io
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date as Date, datetime
from zoneinfo import ZoneInfo

from briany.index_names import index_key
from briany.nse import NseError

URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
REQUIRED = ("Index Name", "Index Date", "Closing Index Value", "Points Change", "Change(%)")
MIN_ROWS = 50                    # real files hold 167 to 169; a short file is a partial download, not a market fact
TTL_RECENT = 3600                # today's and yesterday's file: kept short, in case NSE revises a freshly published file
TTL_PAST = 7 * 86400             # an older day does not change: kept a week, so a repeated question costs no fetch
IST = ZoneInfo("Asia/Kolkata")
_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")


class IndexFileError(NseError):
    """The index file could not be read as the complete file for the requested day. Never carries a URL or body."""


@dataclass(frozen=True)
class IndexRow:
    name: str                    # as NSE publishes it, e.g. 'Nifty 50', 'NIFTY Midcap 100', 'India VIX'
    day: str                     # ISO date of the file
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    points: float | None         # change from the previous close, in index points
    pct: float | None            # change from the previous close, percent
    pe: float | None = None
    pb: float | None = None
    dy: float | None = None      # dividend yield, percent


def _num(text: str) -> float | None:
    """'-.63' -> -0.63, '.89' -> 0.89; '-' or empty -> None. Anything else (including 'nan') is an error, never a number."""
    t = (text or "").strip()
    if t in ("", "-"):
        return None
    if not _NUMBER.fullmatch(t):
        raise IndexFileError("unreadable number in the index file")
    v = float(t)
    if not math.isfinite(v):
        raise IndexFileError("unreadable number in the index file")
    return v


def _iso(text: str) -> str:
    try:
        d, m, y = text.strip().split("-")
        return Date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        raise IndexFileError("unreadable date in the index file") from None


def parse_index_csv(text: str, day: str) -> tuple:
    """Rows of the file for `day` (ISO), or IndexFileError. Fails closed on HTML, a missing column, a short file, a row
    dated another day, or two different rows for one index."""
    text = (text or "").lstrip("\ufeff")
    if not text.strip() or text.lstrip()[:1] == "<":
        raise IndexFileError("the index file is not a CSV (an error page, or empty)")
    reader = list(csv.reader(io.StringIO(text)))
    header = [h.strip() for h in reader[0]] if reader else []
    if any(col not in header for col in REQUIRED):
        raise IndexFileError("the index file has unexpected columns")
    col = {h: i for i, h in enumerate(header)}

    def cell(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    rows, seen = [], {}
    for raw in reader[1:]:
        if not any(c.strip() for c in raw):
            continue
        if len(raw) != len(header):
            raise IndexFileError("a row has the wrong number of columns")       # e.g. an unquoted comma inside a number
        name = cell(raw, "Index Name").strip()
        if not name:
            raise IndexFileError("a row has no index name")
        if _iso(cell(raw, "Index Date")) != day:
            raise IndexFileError("the index file is for a different day")
        row = IndexRow(name, day, _num(cell(raw, "Open Index Value")), _num(cell(raw, "High Index Value")),
                       _num(cell(raw, "Low Index Value")), _num(cell(raw, "Closing Index Value")),
                       _num(cell(raw, "Points Change")), _num(cell(raw, "Change(%)")), _num(cell(raw, "P/E")),
                       _num(cell(raw, "P/B")), _num(cell(raw, "Div Yield")))
        key = index_key(name)
        if key in seen:
            if seen[key] != row:
                raise IndexFileError("the index file lists one index twice with different figures")
            continue
        seen[key] = row
        rows.append(row)
    if len(rows) < MIN_ROWS:
        raise IndexFileError("the index file is incomplete")
    return tuple(rows)


def _pack(rows) -> list:
    return [[r.name, r.open, r.high, r.low, r.close, r.points, r.pct, r.pe, r.pb, r.dy] for r in rows]


def _unpack(day: str, data) -> tuple:
    return tuple(IndexRow(x[0], day, *x[1:]) for x in data)


class IndexClient:
    """Fetches one day's file on demand. `cache` is a MemoryCache or RedisCache from briany.nse (or None)."""

    def __init__(self, cache=None, opener=None, sleep=time.sleep, retries=1, timeout=20, ttl=None, now=None):
        self.cache, self.ttl = cache, ttl                      # ttl: a fixed lifetime for every day (tests); else by age
        self._now = now or (lambda: datetime.now(IST))
        self._open, self._sleep = opener or urllib.request.urlopen, sleep
        self.retries, self.timeout = retries, timeout

    def _ttl_for(self, day: str) -> int:
        """A file for today or yesterday lives an hour; an older one a week. Failures are never cached at all."""
        if self.ttl is not None:
            return self.ttl
        age = (self._now().astimezone(IST).date() - Date.fromisoformat(day)).days
        return TTL_RECENT if age <= 1 else TTL_PAST

    def day(self, day: str) -> tuple:
        """All index rows for the ISO `day`. Raises IndexFileError when the file is missing, blocked or not the full file."""
        try:
            Date.fromisoformat(day)
        except ValueError:
            raise IndexFileError("bad date") from None
        key = f"idx:{day}"
        if self.cache is not None:
            try:
                hit = self.cache.get(key)
            except Exception:  # noqa: BLE001 - a cache fault must never hide the answer
                hit = None
            if hit:
                try:
                    return _unpack(day, hit)
                except (TypeError, ValueError, IndexError):
                    pass                                                   # a damaged entry is ignored and fetched again
        d = Date.fromisoformat(day)
        url = URL.format(ddmmyyyy=f"{d.day:02d}{d.month:02d}{d.year}")
        body = None
        for attempt in range(self.retries + 1):
            try:
                with self._open(urllib.request.Request(url, headers=HEADERS), timeout=self.timeout) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < self.retries:
                    self._sleep(2)
                    continue
                raise IndexFileError(f"HTTP {e.code} from NSE (index file)") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.retries:
                    self._sleep(2)
                    continue
                raise IndexFileError(f"network error ({type(e).__name__}) from NSE (index file)") from None
        rows = parse_index_csv(body.decode("utf-8", "replace"), day)
        if self.cache is not None:
            try:
                self.cache.set(key, _pack(rows), self._ttl_for(day))
            except Exception:  # noqa: BLE001
                pass
        return rows


def find_row(rows, name: str):
    """The row for an index name (compared with index_key, so case and spacing do not matter), or None."""
    want = index_key(name)
    return next((r for r in rows if index_key(r.name) == want), None)
