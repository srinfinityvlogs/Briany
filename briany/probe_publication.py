"""Measure when NSE publishes the end-of-day file for a day. Read-only, public data only.

  python -m briany.probe_publication --date 2026-11-10 --until 22:30
Polls until the file appears or the cut-off time (IST) passes, and logs the first time it was seen.
"""
from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from briany.nsefiles import FetchError, fetch_bhavcopy

IST = ZoneInfo("Asia/Kolkata")
MAX_CONSECUTIVE_ERRORS = 5


def _stamp(t: datetime) -> str:
    return t.astimezone(IST).strftime("%H:%M:%S IST")


def probe(day, fetch=fetch_bhavcopy, now=lambda: datetime.now(IST), sleep=time.sleep,
          until=None, interval=120, log=print):
    """Returns the IST datetime when the file was first seen, or None. Raises FetchError after
    MAX_CONSECUTIVE_ERRORS failures in a row (a block must not look like 'not published yet')."""
    started = now()
    until = until or started.replace(hour=22, minute=30, second=0, microsecond=0)
    log(f"probe started {_stamp(started)} for the {day} file; polling every {interval}s until {_stamp(until)}")
    errors, first = 0, True
    while True:
        t = now()
        if t >= until:
            log(f"[{_stamp(t)}] cut-off reached; the {day} file was not seen")
            return None
        try:
            text = fetch(day)
            errors = 0
        except FetchError as e:
            errors += 1
            log(f"[{_stamp(t)}] fetch problem ({errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                raise
            text = None
        if text is not None:
            where = "already available at the first check" if first else "first seen"
            log(f"[{_stamp(t)}] FILE AVAILABLE ({len(text)} chars): {day} file {where}")
            if first:
                log("  -> published before the probe started, so this is only an UPPER bound on publication time")
            else:
                log(f"  -> published between {_stamp(prev)} and {_stamp(t)}")
            return t
        log(f"[{_stamp(t)}] no file yet")
        prev, first = t, False
        sleep(interval)


def _hhmm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError("use HH:MM (24-hour, IST)")
    return int(m.group(1)), int(m.group(2))


def _iso(text: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=_iso, help="file date (default: today in IST)")
    ap.add_argument("--only-on", type=_iso, help="do nothing unless today (IST) is this date")
    ap.add_argument("--until", type=_hhmm, default=(22, 30), help="stop at this IST time (HH:MM)")
    ap.add_argument("--interval", type=int, default=120)
    a = ap.parse_args(argv)
    today = datetime.now(IST).date().isoformat()
    if a.only_on and a.only_on != today:
        print(f"today (IST) is {today}, not {a.only_on}; nothing to do")
        return
    day = a.date or today
    until = datetime.now(IST).replace(hour=a.until[0], minute=a.until[1], second=0, microsecond=0)
    probe(day, until=until, interval=a.interval)


if __name__ == "__main__":
    main()
