import pytest

from briany.marketdata import (CorporateAction, DataError, PriceRow, daily_move, parse_prices,
                               period_return, period_start, shift_months)
from briany.store import MemoryStore
from datetime import date

from helpers import HEADER


def real_reliance_store():
    s = MemoryStore()
    # Closes and previous closes copied from NSE files (3-6 Nov 2026)
    s.put_day("2026-11-03", {"RELIANCE": PriceRow(1307.90, 1300.00, 1)})
    s.put_day("2026-11-04", {"RELIANCE": PriceRow(1311.00, 1307.90, 19451679)})
    s.put_day("2026-11-05", {"RELIANCE": PriceRow(1313.40, 1311.00, 1)})
    s.put_day("2026-11-06", {"RELIANCE": PriceRow(1320.00, 1313.40, 1)})
    return s


def monthly_store():
    """Weekdays 2026-10-05..2026-11-06; symbol X closes 100,101,102,... ; Y only from 10-20."""
    s, d, i = MemoryStore(), date(2026, 10, 5), 0
    while d <= date(2026, 11, 6):
        if d.weekday() < 5:
            rows = {"X": PriceRow(100.0 + i, 99.0 + i, 1)}
            if d >= date(2026, 10, 20):
                rows["Y"] = PriceRow(50.0, 50.0, 1)
            s.put_day(d.isoformat(), rows)
            i += 1
        d = date.fromordinal(d.toordinal() + 1)
    return s


# ---------------------------------------------------------------- parsing
def test_parse_filters_series_and_keeps_most_traded_duplicate():
    text = (HEADER +
            "2026-11-04,AAA,EQ,10,9,100,1000\n"
            "2026-11-04,AAA,BE,11,9,100,50\n"
            "2026-11-04,BOND,N1,100,100,5,500\n")
    day, rows = parse_prices(text, expected_date="2026-11-04")
    assert day == "2026-11-04" and set(rows) == {"AAA"} and rows["AAA"].close == 10.0


def test_parse_rejects_wrong_mixed_or_empty_files():
    with pytest.raises(DataError):
        parse_prices(HEADER + "2026-11-04,AAA,EQ,10,9,1,10\n", expected_date="2026-11-05")
    with pytest.raises(DataError):
        parse_prices(HEADER + "2026-11-04,AAA,EQ,10,9,1,10\n2026-11-05,BBB,EQ,10,9,1,10\n")
    with pytest.raises(DataError):
        parse_prices(HEADER)


# ---------------------------------------------------------------- daily move
def test_daily_move_latest_uses_file_prev_close_and_checks_chain():
    m = daily_move(real_reliance_store(), "RELIANCE")
    assert (m.status, m.date, m.close, m.prev_close) == ("ok", "2026-11-06", 1320.0, 1313.4)
    assert m.pct == 0.5 and m.prev_matches_store is True and m.note == ""


def test_daily_move_on_date_and_weekend_rolls_back():
    s = real_reliance_store()
    assert daily_move(s, "RELIANCE", "2026-11-04").pct == 0.24
    assert daily_move(s, "RELIANCE", "2026-11-07").date == "2026-11-06"   # Saturday


def test_daily_move_flags_prev_close_mismatch():
    s = MemoryStore()
    s.put_day("2026-11-03", {"ABC": PriceRow(100.0, 100.0, 1)})
    s.put_day("2026-11-04", {"ABC": PriceRow(52.0, 50.0, 1)})   # e.g. 1:1 bonus adjusted base
    m = daily_move(s, "ABC")
    assert m.status == "ok" and m.prev_matches_store is False
    assert "corporate action" in m.note


def test_daily_move_no_data_cases():
    assert daily_move(MemoryStore(), "X").status == "no_data"
    assert daily_move(real_reliance_store(), "NOPE").status == "no_data"
    assert daily_move(real_reliance_store(), "RELIANCE", "2020-01-01").status == "no_data"


# ---------------------------------------------------------------- period returns
def test_one_month_return_ok():
    s = monthly_store()
    start = period_start("2026-11-06", "1m")             # 2026-10-06
    r = period_return(s, "X", "2026-11-06", start)
    assert r.status == "ok" and r.start_date == "2026-10-06" and r.end_date == "2026-11-06"
    assert r.start_close == 101.0 and r.end_close == 124.0     # 25 weekdays from 10-05: index 24
    assert r.pct == round((124.0 - 101.0) / 101.0 * 100, 2)


def test_corporate_action_inside_window_refuses_without_number():
    s = monthly_store()
    act = CorporateAction("2026-10-20", "Bonus 1:1")
    r = period_return(s, "X", "2026-11-06", "2026-10-06", actions=[act])
    assert r.status == "blocked_by_corporate_action" and r.pct is None and r.actions == (act,)


def test_corporate_action_window_boundaries():
    s = monthly_store()
    before = CorporateAction("2026-10-06", "Split")      # ex-date == start day: not inside
    on_end = CorporateAction("2026-11-06", "Split")      # ex-date == end day: inside
    assert period_return(s, "X", "2026-11-06", "2026-10-06", [before]).status == "ok"
    assert period_return(s, "X", "2026-11-06", "2026-10-06", [on_end]).status == "blocked_by_corporate_action"


def test_insufficient_history_and_symbol_gaps():
    s = monthly_store()
    r = period_return(s, "X", "2026-11-06", "2025-01-01")
    assert r.status == "insufficient_history" and "2026-10-05" in r.note and r.pct is None
    y = period_return(s, "Y", "2026-11-06", "2026-10-06")     # Y not listed in start file
    assert y.status == "no_data" and "2026-10-06" in y.note
    assert period_return(s, "X", "2026-11-06", "2026-11-06").status == "no_data"
    assert period_return(MemoryStore(), "X", "2026-11-06", "2026-10-06").status == "no_data"


def test_stored_gap_is_not_papered_over():
    s = MemoryStore()
    s.put_day("2026-09-01", {"X": PriceRow(100.0)})       # nothing near 2026-10-06
    s.put_day("2026-11-06", {"X": PriceRow(120.0)})
    assert period_return(s, "X", "2026-11-06", "2026-10-06").status == "insufficient_history"


# ---------------------------------------------------------------- date helpers
def test_period_start_labels():
    assert period_start("2026-11-06", "1w") == "2026-10-30"
    assert period_start("2026-11-06", "1y") == "2025-11-06"
    assert period_start("2026-11-06", "ytd") == "2025-12-31"
    assert period_start("2026-03-31", "1m") == "2026-02-28"
    assert period_start("2026-11-06", "weird") is None
    assert shift_months(date(2024, 3, 31), 1) == date(2024, 2, 29)
