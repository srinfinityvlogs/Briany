"""Fill the Redis price history from NSE end-of-day files.

  python -m briany.ingest --recent 7
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

from briany.marketdata import MAX_GAP_DAYS, DataError, find_jumps, parse_prices
from briany.nsefiles import FetchError, fetch_bhavcopy
from briany.symbols import parse_bhavcopy


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


def memory_report(client, configured_max_mb: float = 0.0):
    """(used_bytes, max_bytes) or None if the server does not say. Numbers only, no secrets.
    Upstash reports no maxmemory, so fall back to the configured plan cap."""
    try:
        info = client.info("memory")
        used, limit = int(info.get("used_memory", 0)), int(info.get("maxmemory", 0))
    except Exception:  # noqa: BLE001
        return None
    if not limit and configured_max_mb:
        limit = int(configured_max_mb * 1_000_000)
    return used, limit


def prune_history(store, keep_days: int, max_per_run: int = 40):
    """Drop days older than keep_days, measured from the latest stored day (not the clock)."""
    latest = store.latest_day()
    if latest is None:
        return []
    cutoff = (Date.fromisoformat(latest) - timedelta(days=keep_days)).isoformat()
    return store.prune_before(cutoff, max_per_run)


def calendar_days(start: str, end: str):
    """Every calendar day: NSE sometimes holds special weekend sessions (Budget day, Muhurat).
    Ordinary weekends return 404 and are reported as no_file."""
    d, last = Date.fromisoformat(start), Date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def record_jumps(store, day) -> int:
    """Record large one-day moves for `day` against the previous stored day (if close enough)."""
    prior = store.day_on_or_before((Date.fromisoformat(day) - timedelta(days=1)).isoformat())
    if prior is None or (Date.fromisoformat(day) - Date.fromisoformat(prior)).days > MAX_GAP_DAYS:
        return 0
    found = find_jumps(store.get_day(day), store.get_day(prior), day)
    store.add_jumps(found)
    return len(found)


def rebuild_jumps(store) -> int:
    """Recompute every large-move record from the stored days (use after out-of-order backfills)."""
    days, entries = store.all_days(), []
    for prior, day in zip(days, days[1:]):
        if (Date.fromisoformat(day) - Date.fromisoformat(prior)).days > MAX_GAP_DAYS:
            continue
        entries += find_jumps(store.get_day(day), store.get_day(prior), day)
    store.clear_jumps()
    store.add_jumps(entries)
    return len(entries)


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
    record_jumps(store, parsed_day)
    if store.latest_day() == parsed_day:             # names for the symbol resolver come from the newest file
        store.set_instruments(parsed_day, parse_bhavcopy(text)[0])
    return "stored"


def backfill(store, start, end, fetch=fetch_bhavcopy, force=False,
             min_interval=1.0, sleep=time.sleep, log=print) -> Counter:
    counts: Counter = Counter()
    for day in calendar_days(start, end):
        result = ingest_day(store, day, fetch=fetch, force=force, sleep=sleep)
        counts[result] += 1
        log(f"{day}: {result}")
        if result in ("stored", "no_file"):
            sleep(min_interval)            # be polite to NSE
    return counts


def connect():
    """Validate REDIS_URL, connect, ping. Returns (client, store, settings). Never prints the URL."""
    from briany.config import Settings
    settings = Settings.from_env()
    url = settings.redis_url
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
    return client, store, settings


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--recent", type=int, help="ingest the last N calendar days up to today (IST)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="read-only report on what is stored")
    ap.add_argument("--refresh-instruments", action="store_true",
                    help="store the instrument list from the newest stored day's file")
    ap.add_argument("--refresh-actions", action="store_true",
                    help="replace the stored copy of NSE corporate actions")
    ap.add_argument("--rebuild-jumps", action="store_true", help="recompute large-move records from stored days")
    ap.add_argument("--purge-legacy", action="store_true",
                    help="delete the old px:* keys (one-time cleanup), then exit")
    a = ap.parse_args(argv)

    client, store, settings = connect()
    mem = memory_report(client, settings.redis_max_memory_mb)
    if mem:
        used, limit = mem
        print(f"redis memory: {used / 1e6:.1f} MB used of "
              f"{limit / 1e6:.1f} MB" if limit else f"redis memory: {used / 1e6:.1f} MB used (no limit reported)")
    if a.check:
        from briany.dbcheck import run_check
        sys.exit(0 if run_check(store) else 1)
    if a.rebuild_jumps:
        print("large-move records rebuilt:", rebuild_jumps(store))
        return
    if a.refresh_instruments:
        day = store.latest_day()
        text = fetch_bhavcopy(day) if day else None
        if not text:
            sys.exit("no stored day, or its file could not be downloaded")
        instruments = parse_bhavcopy(text)[0]
        store.set_instruments(day, instruments)
        print(f"instrument list stored from the {day} file: {len(instruments)} instruments")
        return
    if a.refresh_actions:
        from briany.nse import NseClient
        from briany.refresh_actions import refresh_actions
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        refresh_actions(store, NseClient(min_interval=settings.nse_min_interval_seconds), today_ist)
        return
    if a.purge_legacy:
        from briany.store import purge_legacy
        print("removed legacy keys:", purge_legacy(client))
        mem = memory_report(client, settings.redis_max_memory_mb)
        if mem:
            print(f"redis memory after cleanup: {mem[0] / 1e6:.1f} MB used")
        return
    if mem and mem[1] and mem[0] > 0.9 * mem[1]:
        sys.exit("Redis is more than 90% full; refusing to write. Run the legacy cleanup or free space.")

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if a.recent:
        end = today.isoformat()
        start = (today - timedelta(days=a.recent - 1)).isoformat()
    else:
        end = a.end or a.start
        start = a.start
        if not start:
            sys.exit("give --recent N, or --start (and optionally --end)")
    counts = backfill(store, start, end, force=a.force)
    print("summary:", dict(counts), "| latest stored day:", store.latest_day())
    if a.recent:                       # retention runs only in the scheduled job
        removed = prune_history(store, settings.price_retention_days)
        print(f"pruned {len(removed)} day(s) older than {settings.price_retention_days} days"
              + (f": {removed[0]} .. {removed[-1]}" if removed else ""))


if __name__ == "__main__":
    main()
