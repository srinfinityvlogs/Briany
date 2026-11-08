import fakeredis
import pytest

from briany.crosscheck import crosscheck, investigate
from briany.ingest import ingest_day, rebuild_jumps, record_jumps
from briany.marketdata import (CorporateAction, PriceRow, daily_move, find_jumps, period_return)
from briany.nse_parsers import CorpAction
from briany.store import MemoryStore, RedisStore

from helpers import make_csv


@pytest.fixture(params=["memory", "redis"])
def store(request):
    return MemoryStore() if request.param == "memory" else RedisStore(fakeredis.FakeRedis(decode_responses=True))


# ---------------------------------------------------------------- detection
def test_find_jumps_threshold_and_penny_filter():
    prior = {"ETF": PriceRow(1000.0), "UP": PriceRow(100.0), "SMALL": PriceRow(100.0),
             "PENNY-RE": PriceRow(0.10), "NEW": PriceRow(50.0)}
    today = {"ETF": PriceRow(100.0), "UP": PriceRow(135.0), "SMALL": PriceRow(110.0),
             "PENNY-RE": PriceRow(0.15), "NEWLISTING": PriceRow(80.0), "NEW": PriceRow(50.0)}
    got = find_jumps(today, prior, "2026-03-06")
    assert sorted(got) == [("ETF", "2026-03-06", -90.0), ("UP", "2026-03-06", 35.0)]


def test_rights_entitlements_are_never_jumps():
    prior = {"OLAELEC-RE": PriceRow(10.0), "INDO-RE2": PriceRow(5.0), "SILGO-RE1": PriceRow(8.0)}
    today = {"OLAELEC-RE": PriceRow(6.0), "INDO-RE2": PriceRow(3.0), "SILGO-RE1": PriceRow(11.2)}
    assert find_jumps(today, prior, "2026-03-06") == []


# ---------------------------------------------------------------- daily move
def two_day_store(c1, c2, prev2=None):
    s = MemoryStore()
    s.put_day("2026-03-05", {"X": PriceRow(c1, c1)})
    s.put_day("2026-03-06", {"X": PriceRow(c2, prev2 if prev2 is not None else c1)})
    return s


def test_daily_move_flags_unlisted_large_move_but_keeps_the_number():
    m = daily_move(two_day_store(14401.8, 143.2), "X")
    assert m.status == "unverified_large_move" and m.pct == -99.01 and "unlisted" in m.note


def test_daily_move_listed_action_beats_unverified_and_small_moves_stay_ok():
    act = [CorporateAction("2026-03-06", "Face Value Split")]
    assert daily_move(two_day_store(1000.0, 100.0), "X", actions=act).status == "blocked_by_corporate_action"
    assert daily_move(two_day_store(100.0, 102.0), "X").status == "ok"
    assert daily_move(two_day_store(100.0, 70.5), "X").status == "ok"                          # -29.5%: below threshold
    assert daily_move(two_day_store(100.0, 70.0), "X").status == "unverified_large_move"       # -30.0%: at threshold
    assert daily_move(two_day_store(0.10, 0.15), "X").status == "ok"                           # penny: ignored


# ---------------------------------------------------------------- period return
def period_store():
    s = MemoryStore()
    for day, close in (("2026-09-01", 100.0), ("2026-10-01", 98.0), ("2026-10-15", 9.8), ("2026-11-02", 10.0)):
        s.put_day(day, {"X": PriceRow(close)})
    return s


def test_period_return_refuses_when_a_jump_is_inside_the_window():
    s = period_store()
    s.add_jumps([("X", "2026-10-15", -90.0)])
    r = period_return(s, "X", "2026-11-02", "2026-09-01")
    assert r.status == "unverified_large_move" and r.pct is None and r.jumps == (("2026-10-15", -90.0),)
    ok = period_return(s, "X", "2026-11-02", "2026-10-15")           # window starts ON the jump day: excluded
    assert ok.status == "ok"
    assert period_return(s, "X", "2026-11-02", "2026-09-01", jumps=[]).status == "ok"      # explicit override


def test_listed_action_takes_precedence_over_jump():
    s = period_store()
    s.add_jumps([("X", "2026-10-15", -90.0)])
    act = [CorporateAction("2026-10-15", "Bonus 9:1")]
    assert period_return(s, "X", "2026-11-02", "2026-09-01", actions=act).status == "blocked_by_corporate_action"


# ---------------------------------------------------------------- storage contract
def test_jump_storage_merges_dedupes_and_clears(store):
    store.add_jumps([("A", "2026-03-06", -90.0), ("A", "2026-01-02", 40.0), ("B", "2026-02-02", -50.0)])
    store.add_jumps([("A", "2026-03-06", -89.5)])                   # same date replaces
    assert store.get_jumps("A") == [("2026-01-02", 40.0), ("2026-03-06", -89.5)]
    assert store.get_jumps("NOPE") == []
    assert set(store.all_jumps()) == {"A", "B"}
    store.add_jumps([])
    store.clear_jumps()
    assert store.all_jumps() == {} and store.get_jumps("A") == []


# ---------------------------------------------------------------- ingest integration
def test_ingest_records_jumps_against_previous_stored_day():
    s = MemoryStore()
    files = {"2026-11-03": make_csv("2026-11-03", {"ETFX": (1000.0, 1000.0, 5), "OK": (50.0, 50.0, 5)}),
             "2026-11-04": make_csv("2026-11-04", {"ETFX": (100.0, 1000.0, 5), "OK": (51.0, 50.0, 5)})}
    for d in files:
        assert ingest_day(s, d, fetch=files.get) == "stored"
    assert s.all_jumps() == {"ETFX": [("2026-11-04", -90.0)]}


def test_record_jumps_skips_big_gaps_and_rebuild_recovers_everything():
    s = MemoryStore()
    s.put_day("2026-06-01", {"X": PriceRow(100.0)})
    s.put_day("2026-11-04", {"X": PriceRow(10.0)})                  # 5-month gap: cannot be judged
    assert record_jumps(s, "2026-11-04") == 0
    s.put_day("2026-11-05", {"X": PriceRow(100.0)})                 # +900% vs yesterday
    s.put_day("2026-11-06", {"X": PriceRow(101.0)})
    s.add_jumps([("STALE", "2026-01-01", 99.0)])
    assert rebuild_jumps(s) == 1
    assert s.all_jumps() == {"X": [("2026-11-05", 900.0)]}          # stale entry gone, gap pair ignored


# ---------------------------------------------------------------- crosscheck additions
def test_crosscheck_ignores_penny_prices_and_returns_unexplained_rows():
    s = MemoryStore()
    s.put_day("2026-01-02", {"ETF": PriceRow(1000.0), "PENNY-RE": PriceRow(0.10), "OLAELEC-RE": PriceRow(10.0)})
    s.put_day("2026-01-05", {"ETF": PriceRow(100.0), "PENNY-RE": PriceRow(0.15), "OLAELEC-RE": PriceRow(6.0)})
    res = crosscheck(s, [], out=lambda *_: None)
    assert res["total"] == 1 and [r[2] for r in res["unmatched_rows"]] == ["ETF"]


def test_investigate_lists_what_nse_shows_and_survives_failures():
    class FakeNse:
        def corporate_actions_all(self, symbol, start, end):
            if symbol == "BOOM":
                raise RuntimeError("HTTP 500")
            if symbol == "SPLITCO":
                return [CorpAction("SPLITCO", "SplitCo", "2026-03-04", None, "Face Value Split From Rs 10 To Rs 2",
                                   "split", True)], {}
            return [], {}
    rows = [(99.0, "2026-03-06", "ETF", 1000.0, 10.0, -99.0), (80.0, "2026-03-06", "SPLITCO", 100.0, 20.0, -80.0),
            (70.0, "2026-03-06", "BOOM", 100.0, 30.0, -70.0)]
    lines = []
    investigate(FakeNse(), rows, out=lines.append)
    text = "\n".join(lines)
    assert "2026-03-06 ETF (-99.0%): nothing listed" in text
    assert "SPLITCO (-80.0%): 2026-03-04 split: Face Value Split" in text
    assert "BOOM (-70.0%): lookup failed (RuntimeError)" in text
