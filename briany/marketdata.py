"""Prices from NSE end-of-day files: parsing, daily moves, period returns.

Pure logic, no network. Every figure carries the file date it came from.
Returns follow Option A: if a corporate action falls inside the window the
result is a refusal with no number (closes are not adjusted for splits/bonuses).
"""
from __future__ import annotations

import calendar
import csv
import io
from dataclasses import dataclass
from datetime import date as Date, timedelta

from briany.symbols import ALLOWED_SERIES, is_entitlement

JUMP_PCT = 30.0           # one-day change at/above this is 'large'
MIN_JUMP_PRICE = 1.0      # ignore prior closes below Rs 1: one tick looks like +50% (rights entitlements)
MAX_GAP_DAYS = 7          # nearest stored day may be at most this far from the target
PREV_CLOSE_TOLERANCE = 0.005


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceRow:
    close: float
    prev_close: float | None = None
    volume: int | None = None


@dataclass(frozen=True)
class CorporateAction:
    ex_date: str          # ISO date
    purpose: str


@dataclass(frozen=True)
class DailyMove:
    status: str           # "ok" | "no_data" | "blocked_by_corporate_action" | "unverified_large_move"
    symbol: str
    date: str | None = None
    close: float | None = None
    prev_close: float | None = None
    pct: float | None = None
    prev_matches_store: bool | None = None   # None = prior day not stored
    note: str = ""
    actions: tuple = ()


@dataclass(frozen=True)
class ReturnResult:
    status: str           # "ok" | "no_data" | "insufficient_history" | "blocked_by_corporate_action" | "unverified_large_move"
    symbol: str
    pct: float | None = None
    start_date: str | None = None
    start_close: float | None = None
    end_date: str | None = None
    end_close: float | None = None
    actions: tuple = ()
    note: str = ""
    jumps: tuple = ()     # (date, pct) large one-day moves inside the window


def _num(text) -> float | None:
    try:
        v = float((text or "").strip())
        return v
    except ValueError:
        return None


def parse_prices(text: str, expected_date: str | None = None):
    """Parse NSE CM bhavcopy CSV text -> (iso_date, {symbol: PriceRow})."""
    best: dict[str, tuple[float, PriceRow]] = {}
    day = None
    for r in csv.DictReader(io.StringIO(text)):
        d = (r.get("TradDt") or "").strip()
        if day is None:
            day = d
        elif d != day:
            raise DataError(f"file mixes dates: {day} and {d}")
        if r.get("SctySrs") not in ALLOWED_SERIES or not (r.get("TckrSymb") or "").strip():
            continue
        close = _num(r.get("ClsPric"))
        if close is None or close <= 0:
            continue
        vol = _num(r.get("TtlTradgVol"))
        row = PriceRow(close, _num(r.get("PrvsClsgPric")), int(vol) if vol is not None else None)
        turnover = _num(r.get("TtlTrfVal")) or 0.0
        sym = r["TckrSymb"].strip()
        if sym not in best or turnover > best[sym][0]:
            best[sym] = (turnover, row)
    if not day:
        raise DataError("empty file")
    if expected_date and day != expected_date:
        raise DataError(f"expected file for {expected_date} but it contains {day}")
    return day, {s: row for s, (_, row) in best.items()}


def find_jumps(rows, prior_rows, day, threshold=JUMP_PCT):
    """Large one-day moves between two stored days: [(symbol, day, pct)]. Price-only, so it also
    catches splits/unit changes that NSE's corporate-action feed does not list (e.g. ETFs)."""
    out = []
    for sym, row in rows.items():
        p = prior_rows.get(sym)
        if not p or p.close < MIN_JUMP_PRICE or is_entitlement(sym):
            continue
        pct = (row.close - p.close) / p.close * 100
        if abs(pct) >= threshold:
            out.append((sym, day, round(pct, 1)))
    return out


def _pct(new: float, old: float) -> float:
    return round((new - old) / old * 100, 2)


def daily_move(store, symbol: str, on: str | None = None, actions=()) -> DailyMove:
    """Move on the latest stored day (or the last stored day on/before `on`).
    NSE's previous close is NOT adjusted for splits/bonuses, so a blocking action whose
    ex-date is this day makes the raw change meaningless: no percentage is given."""
    day = store.latest_day() if on is None else store.day_on_or_before(on)
    if day is None:
        return DailyMove("no_data", symbol, note="no price data stored")
    row = store.get_row(day, symbol)
    if row is None:
        return DailyMove("no_data", symbol, date=day, note=f"{symbol} not in the {day} file")
    if not row.prev_close:
        return DailyMove("no_data", symbol, date=day, close=row.close,
                         note="file has no previous close for this symbol")
    prior = store.day_on_or_before((Date.fromisoformat(day) - timedelta(days=1)).isoformat())
    hits = tuple(a for a in actions if (prior is None or a.ex_date > prior) and a.ex_date <= day)
    if hits:
        return DailyMove("blocked_by_corporate_action", symbol, day, row.close, row.prev_close,
                         actions=hits,
                         note="NSE's previous close is not adjusted, so the raw change is not a real move")
    matches = None
    note = ""
    if prior is not None:
        prow = store.get_row(prior, symbol)
        if prow is not None:
            matches = abs(prow.close - row.prev_close) <= PREV_CLOSE_TOLERANCE
            if not matches:
                note = (f"NSE's previous close ({row.prev_close}) differs from the {prior} "
                        f"close ({prow.close}); a corporate action adjustment is possible")
    pct = _pct(row.close, row.prev_close)
    if abs(pct) >= JUMP_PCT and row.prev_close >= MIN_JUMP_PRICE:
        return DailyMove("unverified_large_move", symbol, day, row.close, row.prev_close, pct, matches,
                         "large one-day change with no matching corporate action in NSE's feed; "
                         "it may reflect an unlisted unit or face-value change")
    return DailyMove("ok", symbol, day, row.close, row.prev_close, pct, matches, note)


def shift_months(d: Date, months: int) -> Date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    return Date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def period_start(end: str, label: str) -> str | None:
    """Target start date for labels 1w, 1m, 3m, 6m, 1y, 3y, 5y, ytd. None if unknown."""
    e = Date.fromisoformat(end)
    label = label.lower()
    months = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}
    if label == "1w":
        return (e - timedelta(days=7)).isoformat()
    if label == "ytd":
        return Date(e.year - 1, 12, 31).isoformat()
    if label in months:
        return shift_months(e, months[label]).isoformat()
    return None


def period_return(store, symbol: str, end: str, start_target: str, actions=(), jumps=None) -> ReturnResult:
    end_day = store.day_on_or_before(end)
    start_day = store.day_on_or_before(start_target)
    earliest = store.earliest_day()
    if end_day is None:
        return ReturnResult("no_data", symbol, note="no price data stored")
    if start_day is None or (Date.fromisoformat(start_target) - Date.fromisoformat(start_day)).days > MAX_GAP_DAYS:
        return ReturnResult("insufficient_history", symbol, end_date=end_day,
                            note=f"stored history starts {earliest}; need data near {start_target}")
    if start_day == end_day:
        return ReturnResult("no_data", symbol, start_date=start_day, end_date=end_day,
                            note="start and end fall on the same trading day")
    s, e = store.get_row(start_day, symbol), store.get_row(end_day, symbol)
    if s is None or e is None:
        missing = start_day if s is None else end_day
        return ReturnResult("no_data", symbol, start_date=start_day, end_date=end_day,
                            note=f"{symbol} not in the {missing} file")
    hits = tuple(a for a in actions if start_day < a.ex_date <= end_day)
    if hits:
        return ReturnResult("blocked_by_corporate_action", symbol, start_date=start_day,
                            end_date=end_day, actions=hits,
                            note="closes are not adjusted for splits/bonuses, so no return is given")
    if jumps is None:
        jumps = store.get_jumps(symbol) if hasattr(store, "get_jumps") else ()
    jhits = tuple((d, p) for d, p in jumps if start_day < d <= end_day)
    if jhits:
        return ReturnResult("unverified_large_move", symbol, start_date=start_day, end_date=end_day,
                            jumps=jhits, note="a one-day change of 30% or more inside this period has no matching "
                            "corporate action; it may be an unlisted split or unit change, so no return is given")
    return ReturnResult("ok", symbol, _pct(e.close, s.close), start_day, s.close, end_day, e.close)
