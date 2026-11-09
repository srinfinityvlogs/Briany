import fakeredis
import pytest

from briany.marketdata import dividends_in, load_blockers
from briany.nse import NseError
from briany.nse_parsers import CorpAction, parse_detail
from briany.refresh_actions import refresh_actions
from briany.store import MemoryStore, RedisStore, StoredAction


@pytest.fixture(params=["memory", "redis"])
def store(request):
    return MemoryStore() if request.param == "memory" else RedisStore(fakeredis.FakeRedis(decode_responses=True))


def act(symbol, ex, kind, subject, blocking):
    return CorpAction(symbol, symbol, ex, None, subject, kind, blocking, detail=parse_detail(kind, subject))


ACTIONS = [
    act("AAA", "2026-06-05", "dividend", "Dividend - Rs 6 Per Share", False),
    act("AAA", "2025-12-01", "bonus", "Bonus 1:1", True),
    act("BBB", "2026-03-04", "split", "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", True),
    act("LONG", "2026-01-01", "dividend", "Dividend - Rs 2 Per Share " + "x" * 200, False),
]
META = {"updated": "2026-11-06", "start": "2025-10-03", "end": "2027-01-05", "count": 4, "blocking": 2, "symbols": 3}


# ---------------------------------------------------------------- storage contract
def test_roundtrip_sorted_with_amounts_and_trimmed_subjects(store):
    store.set_actions(ACTIONS, META)
    a = store.get_actions("AAA")
    assert a == [StoredAction("2025-12-01", "bonus", True, "Bonus 1:1", None),
                 StoredAction("2026-06-05", "dividend", False, "Dividend - Rs 6 Per Share", 6.0)]
    assert store.get_actions("BBB")[0].blocking and store.get_actions("BBB")[0].kind == "split"
    assert len(store.get_actions("LONG")[0].subject) == 100
    assert store.get_actions("UNKNOWN") == []
    assert store.get_actions_meta() == META


def test_setting_again_replaces_the_whole_copy(store):
    store.set_actions(ACTIONS, META)
    store.set_actions([act("CCC", "2026-07-01", "bonus", "Bonus 2:1", True)], {**META, "count": 1})
    assert store.get_actions("AAA") == [] and store.get_actions("CCC")[0].kind == "bonus"
    assert store.get_actions_meta()["count"] == 1


def test_empty_copy_clears_and_large_copy_chunks(store):
    store.set_actions(ACTIONS, META)
    store.set_actions([], {**META, "count": 0})
    assert store.get_actions("AAA") == []
    many = [act(f"S{i:04d}", "2026-03-04", "bonus", "Bonus 1:1", True) for i in range(1200)]
    store.set_actions(many, {**META, "count": 1200})
    assert store.get_actions("S0000")[0].kind == "bonus" and store.get_actions("S1199")[0].blocking


def test_no_meta_before_first_load(store):
    assert store.get_actions_meta() is None and store.get_actions("AAA") == []


# ---------------------------------------------------------------- guards used by answers
def test_load_blockers_only_returns_blocking_actions_when_fresh(store):
    store.set_actions(ACTIONS, META)
    blockers, status = load_blockers(store, "AAA", today="2026-11-08")
    assert status == "ok" and [(b.ex_date, b.purpose) for b in blockers] == [("2025-12-01", "Bonus 1:1")]
    assert load_blockers(store, "NOSUCH", "2026-11-08") == ([], "ok")      # loaded, nothing listed


def test_missing_or_stale_copy_is_never_treated_as_no_actions(store):
    blockers, status = load_blockers(store, "AAA", "2026-11-08")
    assert blockers == [] and status.startswith("actions_unavailable") and "never been loaded" in status
    store.set_actions(ACTIONS, META)                                       # updated 2026-11-06
    assert load_blockers(store, "AAA", "2026-11-09")[1] == "ok"            # age 3: allowed
    stale = load_blockers(store, "AAA", "2026-11-10")[1]                   # age 4: refuse
    assert stale.startswith("actions_unavailable") and "2026-11-06" in stale


def test_dividends_in_window(store):
    store.set_actions(ACTIONS, META)
    assert dividends_in(store, "AAA", "2026-01-01", "2026-11-06") == [("2026-06-05", 6.0)]
    assert dividends_in(store, "AAA", "2026-06-05", "2026-11-06") == []    # start is exclusive
    assert dividends_in(store, "BBB", "2025-01-01", "2026-12-31") == []    # splits are not dividends


# ---------------------------------------------------------------- refresh job
class FakeNse:
    def __init__(self, actions=None, error=None):
        self.actions, self.error, self.calls = actions, error, []

    def corporate_actions_all(self, symbol=None, start=None, end=None, strict=False, **kw):
        self.calls.append((start, end, strict))
        if self.error:
            raise self.error
        return self.actions, {"equities": {"raw": 3, "parsed": 3}, "sme": {"raw": 1, "parsed": 1}, "mf": {"raw": 0, "parsed": 0}}


def test_refresh_stores_copy_with_meta_and_asks_strictly():
    s, nse, lines = MemoryStore(), FakeNse(ACTIONS), []
    meta = refresh_actions(s, nse, "2026-11-08", min_records=3, log=lines.append)
    assert nse.calls == [("2025-10-04", "2027-01-07", True)]               # 400 days back, 60 ahead, strict
    assert (meta["count"], meta["blocking"], meta["symbols"], meta["updated"]) == (4, 2, 3, "2026-11-08")
    assert s.get_actions_meta()["updated"] == "2026-11-08" and s.get_actions("BBB")
    assert lines and "4 records (2 blocking)" in lines[0]


def test_failed_or_implausible_refresh_keeps_the_previous_copy():
    s = MemoryStore()
    s.set_actions(ACTIONS, META)
    with pytest.raises(NseError):
        refresh_actions(s, FakeNse(error=NseError("HTTP 403 from NSE")), "2026-11-08", log=lambda *_: None)
    with pytest.raises(RuntimeError, match="keeping the previous copy"):
        refresh_actions(s, FakeNse([ACTIONS[0]]), "2026-11-08", log=lambda *_: None)      # 1 < 500
    assert s.get_actions_meta() == META and s.get_actions("AAA")[0].kind == "bonus"
