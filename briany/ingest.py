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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from briany.marketdata import DataError, parse_prices
from briany.nsefiles import FetchError, fetch_bhavcopy


def redis_url_problem(url: str):
    """None if the URL looks usable, else a hint. Never echoes the value (it holds a password)."""
    if url.startswith(("redis://", "rediss://")):
        return None
    if url.startswith(("http://", "https://")):
        hint = "it looks like a REST (https://) URL; use the connection string starting with rediss://"
    elif " " in url:
        hint = "it contains spaces (a whole command pasted?); paste only the rediss://... string"
    elif url[:1] in ("'", '"'):
        hint = "it starts with a quote character; paste it without quotes"
    elif url.upper().startswith(("REDIS_URL=", "KV_REDIS_URL=")):
        hint = "remove the NAME= prefix; the value must start with rediss://"
    else:
        hint = ("it does not start with redis:// or rediss://. If you only have a host and a "
                "password, build rediss://default:PASSWORD@HOST:6379")
    return f"REDIS_URL is set but invalid: {hint}. (value not shown)"


def redis_url_structure_problem(url: str):
    """Structural check so redis-py never raises an error that echoes the secret."""
    shape = "expected redis[s]://default:PASSWORD@HOST:PORT"
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return f"REDIS_URL is set but invalid: {shape}, but there is no @HOST:PORT part. (value not shown)"
    try:
        host, _port = parts.hostname, parts.port
    except ValueError:
        return f"REDIS_URL is set but invalid: the port is not a number; {shape}. (value not shown)"
    if not host:
        return f"REDIS_URL is set but invalid: the host is missing; {shape}. (value not shown)"
    return None


def memory_report(client):
    """(used_bytes, max_bytes) or None if the server does not say. Numbers only, no secrets."""
    try:
        info = client.info("memory")
        return int(info.get("used_memory", 0)), int(info.get("maxmemory", 0))
    except Exception:  # noqa: BLE001
        return None


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
    ap.add_argument("--purge-legacy", action="store_true",
                    help="delete the old px:* keys (one-time cleanup), then exit")
    a = ap.parse_args(argv)

    from briany.config import Settings
    url = Settings.from_env().redis_url
    if not url:
        sys.exit("REDIS_URL (or KV_REDIS_URL) is not set")
    problem = redis_url_problem(url) or redis_url_structure_problem(url)
    if problem:
        sys.exit(problem)
    import redis
    from briany.store import RedisStore
    try:
        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=15)
        client.ping()
    except Exception as e:  # noqa: BLE001 - library messages can contain the secret
        sys.exit(f"Could not connect to Redis ({type(e).__name__}). Check host, port and password "
                 "in REDIS_URL. Details are hidden because they can contain the secret.")
    store = RedisStore(client)
    mem = memory_report(client)
    if mem:
        used, limit = mem
        print(f"redis memory: {used / 1e6:.1f} MB used of "
              f"{limit / 1e6:.1f} MB" if limit else f"redis memory: {used / 1e6:.1f} MB used (no limit reported)")
    if a.purge_legacy:
        from briany.store import purge_legacy
        print("removed legacy keys:", purge_legacy(client))
        mem = memory_report(client)
        if mem:
            print(f"redis memory after cleanup: {mem[0] / 1e6:.1f} MB used")
        return
    if mem and mem[1] and mem[0] > 0.9 * mem[1]:
        sys.exit("Redis is more than 90% full; refusing to write. Run the legacy cleanup or free space.")

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
