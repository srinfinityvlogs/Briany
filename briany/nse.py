"""NSE client: throttled, cached, loud on blocks. Stdlib only.

Only HTTP 5xx and network errors are retried. A 403 or an HTML page instead of JSON
raises NseError at once, so a block can never be mistaken for 'no data'.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
import urllib.error
import urllib.parse
import urllib.request

from briany import nse_parsers as P

BASE = "https://www.nseindia.com/api/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
ALL_ACTION_INDEXES = ("equities", "sme", "mf")
MAX_CACHE_BYTES = 100_000     # never park big responses in Redis (30 MB plan)

TTL_ACTIONS, TTL_ANNOUNCEMENTS, TTL_MEETINGS, TTL_CALENDAR, TTL_FLOWS = 6 * 3600, 600, 3600, 6 * 3600, 1800


class NseError(RuntimeError):
    pass


class MemoryCache:
    def __init__(self, clock=time.time):
        self._d, self._clock = {}, clock

    def get(self, key):
        item = self._d.get(key)
        if not item or item[0] < self._clock():
            self._d.pop(key, None)
            return None
        return item[1]

    def set(self, key, value, ttl):
        self._d[key] = (self._clock() + ttl, value)


class RedisCache:
    def __init__(self, client, prefix="cache:"):
        self.r, self.prefix = client, prefix        # client created with decode_responses=True

    def get(self, key):
        v = self.r.get(self.prefix + key)
        return json.loads(v) if v else None

    def set(self, key, value, ttl):
        self.r.set(self.prefix + key, json.dumps(value), ex=int(ttl))


class NseClient:
    def __init__(self, cache=None, min_interval=1.0, opener=None, sleep=time.sleep,
                 clock=time.monotonic, retries=1, timeout=30):
        self.cache, self.min_interval = cache, min_interval
        self._open = opener or urllib.request.urlopen
        self._sleep, self._clock, self.retries, self.timeout = sleep, clock, retries, timeout
        self._last = None

    def _throttle(self):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get_json(self, path, params=None, ttl=0):
        items = list((params or {}).items())
        url = BASE + path + ("?" + urllib.parse.urlencode(items) if items else "")
        key = "nse:" + path + "?" + urllib.parse.urlencode(sorted(items))
        if self.cache is not None and ttl:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        body = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with self._open(urllib.request.Request(url, headers=HEADERS), timeout=self.timeout) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < self.retries:
                    self._sleep(2)
                    continue
                raise NseError(f"HTTP {e.code} from NSE ({path})") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.retries:
                    self._sleep(2)
                    continue
                raise NseError(f"network error ({type(e).__name__}) from NSE ({path})") from None
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            raise NseError(f"NSE returned a non-JSON page ({path}); possibly blocked") from None
        if self.cache is not None and ttl and len(body) <= MAX_CACHE_BYTES:
            self.cache.set(key, data, ttl)
        return data

    # --- typed accessors -------------------------------------------------------------
    def _wrap(self, parser, data, what):
        try:
            return parser(data)
        except P.ShapeError as e:
            raise NseError(f"unexpected NSE response for {what}: {e}") from None

    @staticmethod
    def _window(start, end):
        p = {}
        if start:
            p["from_date"] = P.to_nse_date(start)
        if end:
            p["to_date"] = P.to_nse_date(end)
        return p

    def _action_params(self, symbol, start, end, index):
        return {"index": index, **({"symbol": symbol} if symbol else {}), **self._window(start, end)}

    def corporate_actions(self, symbol=None, start=None, end=None, index="equities"):
        data = self.get_json("corporates-corporateActions", self._action_params(symbol, start, end, index), TTL_ACTIONS)
        return self._wrap(P.parse_corporate_actions, data, "corporate actions")

    def corporate_actions_all(self, symbol=None, start=None, end=None, indexes=ALL_ACTION_INDEXES, strict=False):
        """Merge several NSE indexes (equities plus the SME and mutual-fund/ETF listings).
        Returns (actions, report). report[index] = {'raw': n, 'parsed': m} or {'error': text}.
        The first index must succeed; a failing later index is reported, not fatal, unless
        strict=True (used when replacing the stored copy: partial data must never replace it).
        Index names 'equities', 'sme' and 'mf' are verified against real data (Nov 2026)."""
        seen, out, report = set(), [], {}
        for i, index in enumerate(indexes):
            try:
                raw = self.get_json("corporates-corporateActions",
                                    self._action_params(symbol, start, end, index), TTL_ACTIONS)
                parsed = self._wrap(P.parse_corporate_actions, raw, f"corporate actions ({index})")
            except NseError as e:
                if i == 0 or strict:
                    raise
                report[index] = {"error": str(e)}
                continue
            report[index] = {"raw": len(raw) if isinstance(raw, list) else None, "parsed": len(parsed)}
            for a in parsed:
                key = (a.symbol, a.ex_date, a.subject)
                if key not in seen:
                    seen.add(key)
                    out.append(a)
        return sorted(out, key=lambda a: (a.ex_date, a.symbol)), report

    def announcements(self, symbol, start=None, end=None):
        params = {"index": "equities", "symbol": symbol, **self._window(start, end)}
        return self._wrap(P.parse_announcements,
                          self.get_json("corporate-announcements", params, TTL_ANNOUNCEMENTS), "announcements")

    def board_meetings(self, symbol, start=None, end=None):
        params = {"index": "equities", "symbol": symbol, **self._window(start, end)}
        return self._wrap(P.parse_board_meetings,
                          self.get_json("corporate-board-meetings", params, TTL_MEETINGS), "board meetings")

    def event_calendar(self, symbol=None, start=None, end=None):
        params = {"index": "equities", **({"symbol": symbol} if symbol else {}), **self._window(start, end)}
        return self._wrap(P.parse_event_calendar,
                          self.get_json("event-calendar", params, TTL_CALENDAR), "event calendar")

    def next_results(self, symbol, today, horizon_days=120):
        """Next scheduled results meeting on/after `today` (ISO), or None if NSE lists none.
        None means 'not announced', never 'no results'."""
        end = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
        return P.next_results_date(self.event_calendar(symbol, today, end), today)

    def fii_dii(self):
        return self._wrap(P.parse_fii_dii, self.get_json("fiidiiTradeReact", None, TTL_FLOWS), "FII/DII")

    def large_deals(self):
        return self._wrap(P.parse_large_deals, self.get_json("snapshot-capital-market-largedeal", None, TTL_FLOWS),
                          "large deals")
