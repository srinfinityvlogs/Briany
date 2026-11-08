import json
from collections import Counter
from pathlib import Path

import pytest

from briany import nse_parsers as P
from briany.marketdata import PriceRow, daily_move, period_return
from briany.store import MemoryStore

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())["sample"]


# ---------------------------------------------------------------- dates
def test_parse_date_and_datetime():
    assert P.parse_date("05-Jun-2026") == "2026-06-05"
    assert P.parse_date("06-NOV-2026") == "2026-11-06"
    assert P.parse_date("5-jun-2026") == "2026-06-05"
    for bad in ("-", "", None, "31-Feb-2026", "2026-06-05", "05-Foo-2026"):
        assert P.parse_date(bad) is None
    assert P.parse_datetime("06-Nov-2026 19:10:58") == "2026-11-06T19:10:58"
    assert P.parse_datetime("16-Jan-2025 20:20") == "2025-01-16T20:20:00"
    assert P.parse_datetime("garbage") is None
    assert P.to_nse_date("2026-11-06") == "06-11-2026"


# ---------------------------------------------------------------- classification (real subjects)
CASES = [
    ("Bonus 1:1", "bonus", True),
    (" Bonus 1:1", "bonus", True),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", "split", True),
    ("Rights 1:15 @ Premium Rs 1247", "rights", True),
    ("Rights 3:8 @ Premium Re 0.50/-", "rights", True),
    ("Demerger", "restructuring", True),
    ("Consolidation of shares", "consolidation", True),
    ("Capital Reduction", "capital_reduction", True),
    ("Dividend - Rs 6 Per Share", "dividend", False),
    ("Interim Dividend - Rs 4 Per Share", "dividend", False),
    (" Dividend -  Rs 11/- Per Share", "dividend", False),
    ("Annual General Meeting/Dividend - Rs 6.50 Per Share", "dividend", False),
    ("Dividend Rs. 9.50 Per Equity Share", "dividend", False),
    ("Distribution - Rs 3.60 Per Unit Consisting Of Interest Rs 2.72 Per Unit", "distribution", False),
    ("Interest Payment", "interest", False),
    ("Annual General Meeting", "meeting", False),
    ("Buyback of equity shares", "buyback", False),
    ("Something Entirely New", "other", True),       # unknown -> fail closed
    ("", "other", True),
]


@pytest.mark.parametrize("subject,kind,blocking", CASES)
def test_classify_subject(subject, kind, blocking):
    assert P.classify_subject(subject) == (kind, blocking)


def test_parse_detail_numbers():
    assert P.parse_detail("bonus", "Bonus 1:1") == {"ratio": "1:1"}
    assert P.parse_detail("rights", "Rights 1:15 @ Premium Rs 1247") == {"ratio": "1:15"}
    assert P.parse_detail("split", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share") == {"from": 10.0, "to": 5.0}
    assert P.parse_detail("dividend", "Dividend - Rs 6 Per Share") == {"amount": 6.0}
    assert P.parse_detail("dividend", "Interim Dividend - Re 0.40  Per Sh") == {"amount": 0.4}
    assert P.parse_detail("dividend", "Annual General Meeting And Dividend Rs.8.50 Per Share") == {"amount": 8.5}
    assert P.parse_detail("dividend", "Dividend Rs. 9.50 Per Equity Share") == {"amount": 9.5}
    assert P.parse_detail("dividend", " Dividend -  Rs 11/- Per Share") == {"amount": 11.0}
    assert P.parse_detail("dividend", "Dividend") == {}


# ---------------------------------------------------------------- corporate actions (real fixtures)
def test_reliance_history_has_exactly_the_known_price_breaking_actions():
    acts = P.parse_corporate_actions(load("corp_actions_symbol"))
    assert len(acts) == 20 and acts[0].ex_date == "2011-05-05" and acts[-1].ex_date == "2026-06-05"
    blocking = [a for a in acts if a.blocking]
    assert Counter(a.kind for a in blocking) == {"bonus": 2, "rights": 1, "restructuring": 1}
    assert sorted(a.ex_date for a in blocking if a.kind == "bonus") == ["2017-09-07", "2024-10-28"]
    last = acts[-1]
    assert (last.kind, last.blocking, last.detail) == ("dividend", False, {"amount": 6.0})
    assert all(a.symbol == "RELIANCE" and a.isin == "INE002A01018" for a in acts)
    assert acts[0].record_date is None                          # older records have rec "-"


def test_range_fixtures_parse_and_cover_other_series():
    week = P.parse_corporate_actions(load("corp_actions_range_week"))
    assert len(week) == 25 and sum(a.blocking for a in week) == 2
    assert {a.series for a in week} >= {"EQ", "IV", "RR"}
    year = P.parse_corporate_actions(load("corp_actions_range_year"))
    beml = next(a for a in year if a.symbol == "BEML")
    assert (beml.kind, beml.blocking, beml.detail) == ("split", True, {"from": 10.0, "to": 5.0})
    gsec = next(a for a in year if a.symbol == "761GS2030")
    assert (gsec.kind, gsec.blocking) == ("interest", False)


def test_malformed_records_are_skipped_and_bad_shapes_raise():
    good = {"symbol": "ABC", "exDate": "01-Jun-2026", "subject": "Bonus 2:1", "comp": "ABC Ltd"}
    out = P.parse_corporate_actions([good, {"symbol": "X"}, {"exDate": "01-Jun-2026"}, "junk", None])
    assert [a.symbol for a in out] == ["ABC"]
    with pytest.raises(P.ShapeError):
        P.parse_corporate_actions({"data": []})
    with pytest.raises(P.ShapeError):
        P.parse_large_deals([])


# ---------------------------------------------------------------- announcements, meetings, calendar
def test_announcements_newest_first_with_clean_text():
    an = P.parse_announcements(load("announcements_window"))
    assert len(an) == 9 and an[0].published == "2026-11-06T19:10:58"
    assert [a.published for a in an] == sorted((a.published for a in an), reverse=True)
    assert Counter(a.category for a in an)["Analysts/Institutional Investor Meet/Con. Call Updates"] == 3
    assert all(a.symbol == "RELIANCE" and "  " not in a.text for a in an)
    assert an[0].url.startswith("https://nsearchives.nseindia.com/")


def test_board_meetings_flag_results_related():
    bm = P.parse_board_meetings(load("board_meetings"))
    assert len(bm) == 20 and bm[0].meeting_date == "2026-10-30"
    assert any(m.results_related and m.purpose == "Financial Results" for m in bm)
    bonus = [m for m in bm if m.purpose == "Bonus"]
    assert bonus and not bonus[0].results_related


def test_event_calendar_and_next_results_date():
    ev = P.parse_event_calendar(load("event_calendar"))
    assert [e.date for e in ev] == sorted(e.date for e in ev)
    nxt = P.next_results_date(ev, "2016-07-01")
    assert nxt.date == "2016-07-15" and "result" in nxt.purpose.lower()
    assert P.next_results_date(ev, "2030-01-01") is None
    skipped_demerger = P.next_results_date(ev, "2005-01-01")      # 2005 entry is a Demerger
    assert skipped_demerger and "result" in skipped_demerger.purpose.lower()


# ---------------------------------------------------------------- flows and deals
def test_fii_dii():
    rows = P.parse_fii_dii(load("fii_dii"))
    by = {r.category: r for r in rows}
    assert by["DII"].net == -5968.7 and by["FII/FPI"].net == 13749.82
    assert by["DII"].buy - by["DII"].sell == pytest.approx(by["DII"].net, abs=0.01)
    assert {r.date for r in rows} == {"2026-11-06"}


def test_large_deals():
    deals = P.parse_large_deals(load("bulk_snapshot"))
    kinds = Counter(d.kind for d in deals)
    assert kinds["block"] == 20 and kinds["bulk"] >= 20
    first = next(d for d in deals if d.kind == "bulk")
    assert (first.symbol, first.side, first.quantity, first.price, first.remarks) == ("AAATECH", "BUY", 84322, 96.19, "")
    assert {d.date for d in deals if d.kind in ("bulk", "block")} == {"2026-11-06"}
    shorts = [d for d in deals if d.kind == "short"]
    assert {d.date for d in shorts} == {"2026-11-05"}              # short-selling data lags a day
    assert all(d.side == "" and d.client == "" and d.price is None for d in shorts)


# ---------------------------------------------------------------- integration with price logic
def test_bonus_blocks_period_return_but_clean_windows_pass():
    acts = P.blockers(P.parse_corporate_actions(load("corp_actions_symbol")))
    assert len(acts) == 4                                   # dividends are not blockers
    s = MemoryStore()
    for day, close in (("2024-10-01", 3000.0), ("2025-01-02", 1200.0), ("2025-11-03", 1300.0)):
        s.put_day(day, {"RELIANCE": PriceRow(close)})
    r = period_return(s, "RELIANCE", "2025-01-02", "2024-10-01", acts)
    assert r.status == "blocked_by_corporate_action" and r.pct is None and "Bonus 1:1" in r.actions[0].purpose
    assert period_return(s, "RELIANCE", "2025-11-03", "2025-01-02", acts).status == "ok"


def test_daily_move_refuses_on_ex_date_but_not_for_dividends():
    acts = P.blockers(P.parse_corporate_actions(load("corp_actions_symbol")))
    s = MemoryStore()
    s.put_day("2024-10-25", {"RELIANCE": PriceRow(2900.0, 2890.0)})
    s.put_day("2024-10-28", {"RELIANCE": PriceRow(1450.0, 2900.0)})        # unadjusted previous close
    m = daily_move(s, "RELIANCE", "2024-10-28", actions=acts)
    assert m.status == "blocked_by_corporate_action" and m.pct is None and len(m.actions) == 1
    assert daily_move(s, "RELIANCE", "2024-10-28").pct == -50.0               # without actions: raw, misleading
    s.put_day("2026-06-04", {"RELIANCE": PriceRow(1300.0, 1299.0)})
    s.put_day("2026-06-05", {"RELIANCE": PriceRow(1294.0, 1300.0)})            # ex-dividend day
    assert daily_move(s, "RELIANCE", "2026-06-05", actions=acts).status == "ok"
