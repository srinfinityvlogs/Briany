"""Fill the Redis price history from NSE end-of-day files.

  python -m briany.ingest --recent 5
  python -m briany.ingest --start 2025-11-03 --end 2026-11-06
Needs REDIS_URL (or KV_REDIS_URL). Safe to re-run: stored days are skipped.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

from briany.marketdata import DataError, parse_prices
from briany.nsefiles import FetchError, fetch_bhavcopy


def weekdays(start: str, end: str):
    d, last = Date.fromisoformat(start), Date.fromisoformat(end)
    while d <= last:
        if d.weekday() < 5:
            yield d.isoformat()
        d += timedelta(days=1)


def ingest_day(store, day, fetch=fetch_bhavcopy, force=False, retries=1, sleep=time.sleep) -> str:
    """Returns 'stored', 'exists' or 'no_file'. Raises FetchError/DataError on real problems."""
    if not force and store.has_day(day):
        return "exists"
    for attempt in range(retries + 1):
        try:
            text = fetch(day)
            break
        except FetchError:
            if attempt == retries:
                raise
            sleep(5)
    if text is None:
        return "no_file"
    parsed_day, rows = parse_prices(text, expected_date=day)
    if len(rows) < 500:
        raise DataError(f"only {len(rows)} instruments parsed for {day}; refusing to store")
    store.put_day(parsed_day, rows)
    return "stored"


def backfill(store, start, end, fetch=fetch_bhavcopy, force=False,
             min_interval=1.0, sleep=time.sleep, log=print) -> Counter:
    counts: Counter = Counter()
    for day in weekdays(start, end):
        result = ingest_day(store, day, fetch=fetch, force=force, sleep=sleep)
        counts[result] += 1
        log(f"{day}: {result}")
        if result in ("stored", "no_file"):
            sleep(min_interval)            # be polite to NSE
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--recent", type=int, help="ingest the last N weekdays up to today (IST)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    from briany.config import Settings
    url = Settings.from_env().redis_url
    if not url:
        sys.exit("REDIS_URL (or KV_REDIS_URL) is not set")
    import redis
    from briany.store import RedisStore
    store = RedisStore(redis.Redis.from_url(url, decode_responses=True, socket_timeout=15))

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if a.recent:
        end = today.isoformat()
        start = (today - timedelta(days=a.recent * 2)).isoformat()
        days = list(weekdays(start, end))[-a.recent:]
        start = days[0]
    else:
        end = a.end or a.start
        start = a.start
        if not start:
            sys.exit("give --recent N, or --start (and optionally --end)")
    counts = backfill(store, start, end, force=a.force)
    print("summary:", dict(counts), "| latest stored day:", store.latest_day())


if __name__ == "__main__":
    main()
