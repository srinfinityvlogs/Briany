# SPDX-License-Identifier: Apache-2.0
"""Which trading days did NOT have normal hours (09:15 to 15:30 IST)?

The evidence window places a filing or news item before or after a session's close. That needs the close time, and the price
file does not carry it. Special sessions (Muhurat, a Saturday Budget day, an early or late close) have hours of their own and
NSE announces them by circular; this project has no verified source for their times and never types them in by hand.

So a day that cannot have had normal hours is treated as AMBIGUOUS: items published on that calendar day are neither placed
before nor after its close and are not listed, and the answer says so. Two kinds of day count:
  * every Saturday and Sunday: NSE's regular sessions run Monday to Friday, so a stored weekend day is a special session;
  * any weekday listed in the SPECIAL_SESSION_DAYS environment variable (comma-separated ISO dates, e.g. "2026-11-12,2027-02-01"):
    the maintainer adds a weekday special session there when NSE announces it (set it in Vercel and in the workflow secrets).
"""
from __future__ import annotations

import os
import re
from datetime import date as Date


def special_days_from_env(value: str | None = None) -> frozenset:
    """Valid ISO dates from SPECIAL_SESSION_DAYS; anything unreadable is ignored (a typo must not stop the bot)."""
    raw = os.environ.get("SPECIAL_SESSION_DAYS", "") if value is None else value
    out = set()
    for part in re.split(r"[,\s;]+", raw):
        try:
            out.add(Date.fromisoformat(part).isoformat())
        except ValueError:
            continue
    return frozenset(out)


def is_special_session(day: str, extra=frozenset()) -> bool:
    """True for a weekend day or a day in `extra`: its hours are unknown, so it is ambiguous, not 09:15 to 15:30."""
    try:
        d = Date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return d.weekday() >= 5 or d.isoformat() in extra
