# SPDX-License-Identifier: Apache-2.0
"""NSE sometimes answers a GitHub runner with HTTP 403 (seen on 24 Nov 2026 at 08:13 IST, both feeds, while the runs before and
after were fine), and NSE keeps only the latest day, so the ingest job waits and retries, and a scheduled verify turns red when a
trading day since storage began is missing."""
from datetime import datetime
from types import SimpleNamespace

import fakeredis
import pytest

from briany.flows import IST, KNOWN_GAPS, RETRY_WAITS, ingest_flows, verify_flows
from briany.marketdata import PriceRow
from briany.nse import NseError
from briany.store import MemoryStore, RedisStore
from test_flows import DEALS, FII, NOW, FakeNse as _Fake

BLOCKED = "HTTP 403 from NSE (fiidiiTradeReact)"


class SeqNse:
    """Raises the queued errors for a path before answering; records the order of calls."""

    def __init__(self, errors_by_path):
        self.errors = {k: list(v) for k, v in errors_by_path.items()}
        self.calls = []

    def get_json(self, path, params=None, ttl=0):
        self.calls.append(path)
        if self.errors.get(path):
            raise NseError(self.errors[path].pop(0))
        return FII if path == "fiidiiTradeReact" else DEALS


# ================================================================ patient retries
def test_a_refusal_is_waited_out_and_the_second_try_stores_the_day():
    nse, sleeps, store = SeqNse({"fiidiiTradeReact": [BLOCKED]}), [], MemoryStore()
    lines, errors = ingest_flows(nse, store, now=NOW, sleep=sleeps.append)
    assert errors == 0 and sleeps == [30]
    assert "note FII/DII: NSE refused at first, then answered after 1 retry" in lines
    assert store.get_flow("2026-11-20")["fii"][0][0] == "DII"


def test_two_refusals_then_success_uses_both_waits_and_a_persistent_block_is_an_error():
    sleeps = []
    lines, errors = ingest_flows(SeqNse({"fiidiiTradeReact": [BLOCKED, BLOCKED]}), MemoryStore(), now=NOW, sleep=sleeps.append)
    assert errors == 0 and sleeps == [30, 90] and any("after 2 retries" in l for l in lines)
    sleeps.clear()
    lines, errors = ingest_flows(SeqNse({"fiidiiTradeReact": [BLOCKED] * 3, "snapshot-capital-market-largedeal": [BLOCKED] * 3}),
                                 MemoryStore(), now=NOW, sleep=sleeps.append)
    assert errors == 2 and sleeps == [30, 90, 30, 90]
    assert "ERROR FII/DII: HTTP 403 from NSE (fiidiiTradeReact) (still refused after 2 retries)" in lines
    assert RETRY_WAITS == (30, 90)


def test_a_block_page_is_retried_but_other_errors_are_not():
    sleeps = []
    ingest_flows(SeqNse({"fiidiiTradeReact": ["NSE returned a non-JSON page (fiidiiTradeReact); possibly blocked"]}),
                 MemoryStore(), now=NOW, sleep=sleeps.append)
    assert sleeps == [30]
    sleeps.clear()
    lines, errors = ingest_flows(SeqNse({"fiidiiTradeReact": ["HTTP 503 from NSE (fiidiiTradeReact)"]}), MemoryStore(), now=NOW,
                                 sleep=sleeps.append)
    assert sleeps == [] and errors == 1 and "ERROR FII/DII: HTTP 503 from NSE (fiidiiTradeReact)" in lines
    sleeps.clear()
    ingest_flows(SeqNse({}), MemoryStore(), now=NOW, sleep=sleeps.append)
    assert sleeps == []                                                      # a clean run never waits


def test_waits_can_be_switched_off():
    sleeps = []
    _lines, errors = ingest_flows(SeqNse({"fiidiiTradeReact": [BLOCKED]}), MemoryStore(), now=NOW, sleep=sleeps.append, waits=())
    assert errors == 1 and sleeps == []


# ================================================================ verify
def price_store(days):
    s = MemoryStore()
    for d in days:
        s.put_day(d, {"TCS": PriceRow(100.0, 100.0, 10)})
    return s


def full(rec_extra=None):
    rec = {"fii": [["DII", 1.0, 2.0, -1.0], ["FII/FPI", 3.0, 2.0, 1.0]], "bulk": [["A", "C", "BUY", 1, 2.0, ""]], "block": []}
    rec.update(rec_extra or {})
    return rec


def test_all_days_complete_is_ok_and_days_before_storage_began_are_ignored():
    s = price_store(["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"])
    for d in ("2026-11-20", "2026-11-23"):
        s.put_flow(d, full())
    lines, ok = verify_flows(s)
    assert ok and lines[0] == "checked 2 trading day(s) from 2026-11-20 to 2026-11-23: FII/DII, bulk and block expected on each"
    assert lines[1] == "ok   every trading day since storage began is complete"


def test_a_missing_day_or_kind_is_named_and_turns_the_check_red():
    s = price_store(["2026-11-20", "2026-11-23", "2026-11-25", "2026-11-26"])
    s.put_flow("2026-11-20", full())
    s.put_flow("2026-11-23", full())                                                              # complete
    s.put_flow("2026-11-25", {"fii": full()["fii"], "short": [["A", 5]]})                        # bulk and block missing
    lines, ok = verify_flows(s)
    assert not ok
    assert "PROBLEM 2026-11-25: missing bulk, block" in lines and "PROBLEM 2026-11-26: missing fii, bulk, block" in lines
    assert not any("2026-11-23" in l and "PROBLEM" in l for l in lines)
    assert lines[-1].startswith("A day that is missing cannot be rebuilt")


def test_empty_bulk_or_block_lists_count_as_present_but_an_empty_fii_does_not():
    s = price_store(["2026-11-20", "2026-11-23"])
    s.put_flow("2026-11-20", full())                                       # block == [] (NSE declared none): fine
    s.put_flow("2026-11-23", full({"fii": []}))
    lines, ok = verify_flows(s)
    assert not ok and "PROBLEM 2026-11-23: missing fii" in lines and not any("2026-11-20: missing" in l for l in lines)


def test_weekends_and_listed_special_days_are_not_expected_but_short_rows_are_never_required():
    s = price_store(["2026-11-20", "2026-11-21", "2026-11-23", "2026-11-24"])        # Sat 21 Nov is a weekend session
    s.put_flow("2026-11-20", full())
    s.put_flow("2026-11-23", full())
    s.put_flow("2026-11-24", full())                                                 # no short rows anywhere
    assert verify_flows(s)[1] is True
    s2 = price_store(["2026-11-20", "2026-11-23", "2026-11-24"])
    s2.put_flow("2026-11-20", full())
    s2.put_flow("2026-11-24", full())                                                # 23 Nov missing...
    assert verify_flows(s2)[1] is False
    assert verify_flows(s2, special=frozenset({"2026-11-23"}))[1] is True            # ...unless it is a listed special day


def test_nothing_stored_is_a_problem():
    assert verify_flows(price_store(["2026-11-20"])) == (["PROBLEM no FII/DII record is stored at all"], False)


def test_the_cli_exit_codes(monkeypatch, capsys):
    from briany import flows_ingest
    redis = fakeredis.FakeRedis(decode_responses=True)
    st = RedisStore(redis)
    for d in ("2026-11-20", "2026-11-23"):
        st.put_day(d, {"TCS": PriceRow(100.0, 100.0, 10)})
    st.put_flow("2026-11-20", full())
    monkeypatch.setattr("briany.ingest.connect", lambda: (redis, st, SimpleNamespace(redis_max_memory_mb=30)))
    with pytest.raises(SystemExit) as e:
        flows_ingest.main(["--verify"])
    assert e.value.code == 1 and "PROBLEM 2026-11-23: missing fii, bulk, block" in capsys.readouterr().out
    st.put_flow("2026-11-23", full())
    with pytest.raises(SystemExit) as e:
        flows_ingest.main(["--verify"])
    assert e.value.code == 0 and "ok   every trading day" in capsys.readouterr().out


def test_the_verify_workflow_is_scheduled_off_the_hour_and_runs_the_verify_step_last():
    from pathlib import Path
    import yaml
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "workflows" / "verify-flows.yml").read_text())
    steps = doc["jobs"]["verify"]["steps"]
    assert "--verify" in str(steps[-1].get("run")) and "|| true" in str([x.get("run") for x in steps])
    assert all("run" in x or "uses" in x for x in steps)


# ================================================================ days that were lost for good
LOST = frozenset({"2026-11-24"})                # an example day; real ones go in KNOWN_GAPS or the KNOWN_FLOW_GAPS variable


def gap_store(extra_missing=()):
    """20, 23, 24, 25 and 26 Nov; the 24th has prices but no flows at all."""
    s = price_store(["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"])
    for d in ("2026-11-20", "2026-11-23", "2026-11-25", "2026-11-26"):
        if d not in extra_missing:
            s.put_flow(d, full())
    return s


def test_a_missing_day_that_is_listed_as_a_known_gap_keeps_the_check_green_and_is_named():
    lines, ok = verify_flows(gap_store(), known_gaps=LOST)
    assert ok
    assert lines[0].startswith("checked 5 trading day(s) from 2026-11-20 to 2026-11-26")
    assert any(l.startswith("note 2026-11-24: known gap, not counted (missing fii, bulk, block;") for l in lines)
    assert lines[-1] == "ok   every trading day since storage began is complete, apart from the known gap above"


def test_nothing_is_a_known_gap_unless_listed_so_the_same_day_is_a_problem_by_default():
    assert isinstance(KNOWN_GAPS, frozenset) and not KNOWN_GAPS
    lines, ok = verify_flows(gap_store())
    assert not ok and "PROBLEM 2026-11-24: missing fii, bulk, block" in lines


def test_a_new_missing_day_still_turns_it_red_next_to_a_known_gap():
    lines, ok = verify_flows(gap_store(extra_missing=("2026-11-25",)), known_gaps=LOST)
    assert not ok
    assert "PROBLEM 2026-11-25: missing fii, bulk, block" in lines and not any("PROBLEM 2026-11-24" in l for l in lines)
    assert any(l.startswith("note 2026-11-24: known gap") for l in lines)


def test_several_known_gaps_are_named_in_the_ok_line():
    lines, ok = verify_flows(gap_store(extra_missing=("2026-11-25",)), known_gaps=LOST | {"2026-11-25"})
    assert ok and lines[-1] == "ok   every trading day since storage began is complete, apart from the known gaps above"


def test_a_known_gap_day_that_is_complete_says_nothing():
    s = gap_store()
    s.put_flow("2026-11-24", full())
    lines, ok = verify_flows(s, known_gaps=LOST)
    assert ok and not any("known gap" in l for l in lines) and lines[-1] == "ok   every trading day since storage began is complete"


def test_a_holiday_with_no_prices_is_never_expected():
    s = price_store(["2026-11-20", "2026-11-23", "2026-11-25"])             # no prices for Tuesday 24 Nov: a market holiday
    for d in ("2026-11-20", "2026-11-23", "2026-11-25"):
        s.put_flow(d, full())
    lines, ok = verify_flows(s)
    assert ok and lines[0].startswith("checked 3 trading day(s)") and not any("2026-11-24" in l for l in lines)


def test_the_cli_reads_the_known_gaps_variable(monkeypatch, capsys):
    from briany import flows_ingest
    redis = fakeredis.FakeRedis(decode_responses=True)
    st = RedisStore(redis)
    for d in ("2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"):
        st.put_day(d, {"TCS": PriceRow(100.0, 100.0, 10)})
    for d in ("2026-11-20", "2026-11-23"):
        st.put_flow(d, full())
    monkeypatch.setattr("briany.ingest.connect", lambda: (redis, st, SimpleNamespace(redis_max_memory_mb=30)))
    monkeypatch.delenv("KNOWN_FLOW_GAPS", raising=False)
    with pytest.raises(SystemExit) as e:                                 # nothing is known: 24 and 25 Nov are both problems
        flows_ingest.main(["--verify"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "PROBLEM 2026-11-24" in out and "PROBLEM 2026-11-25" in out
    monkeypatch.setenv("KNOWN_FLOW_GAPS", "2026-11-24")                  # 24 Nov listed; 25 Nov is not
    with pytest.raises(SystemExit) as e:
        flows_ingest.main(["--verify"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "PROBLEM 2026-11-25" in out and "PROBLEM 2026-11-24" not in out and "note 2026-11-24: known gap" in out
    monkeypatch.setenv("KNOWN_FLOW_GAPS", "2026-11-24, nonsense, 2026-11-25")      # an unreadable entry is ignored
    with pytest.raises(SystemExit) as e:
        flows_ingest.main(["--verify"])
    assert e.value.code == 0 and "apart from the known gaps above" in capsys.readouterr().out


def test_the_verify_workflow_passes_the_known_gaps_variable():
    from pathlib import Path
    import yaml
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "workflows" / "verify-flows.yml").read_text())
    last = doc["jobs"]["verify"]["steps"][-1]
    assert last["env"]["KNOWN_FLOW_GAPS"] == "${{ vars.KNOWN_FLOW_GAPS }}" and last["env"]["SPECIAL_SESSION_DAYS"]
