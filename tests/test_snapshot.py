# SPDX-License-Identifier: Apache-2.0
"""The record-and-replay tool behind the Step 9 regression tests, checked on synthetic data."""
import json

import pytest

from briany.news import NewsItem, NewsResult
from briany.nse import NseError
from briany.nse_parsers import Announcement, FiiDii
from briany.snapshot import (PERIODS, Recorder, Replay, build_calls, call_key, capture_snapshot, decode, encode,
                             jsonable, load_snapshot, replay_assembler, run_call, save_snapshot, summarize)
from briany.store import StoredAction
from briany.symbols import Instrument
from test_evidence import FULL_NEWS, FULL_NSE, NOW, FakeNews, FakeNse, act, build_store

INSTRUMENTS = [Instrument("ACME", "ACME INDUSTRIES LTD"), Instrument("TATASTEEL", "TATA STEEL LIMITED"),
               Instrument("TATAPOWER", "TATA POWER CO LTD")]
HISTORY = {"2026-10-09": (90.0, 89.0), "2026-11-02": (95.0, 94.0), "2026-11-05": (100.0, 99.0),
           "2026-11-06": (101.0, 100.0), "2026-11-09": (110.0, 101.0)}
DIVIDEND = act("2026-11-03", "dividend", "Dividend - Rs 5 Per Share", False)
BONUS = act("2026-11-06", "bonus", "Bonus 1:1", True)


def scenarios():
    calls = build_calls(["acme"], ["2026-11-09", "2026-11-06"], periods=("1w", "1m"))
    calls.append({"id": "why:nonsense", "fn": "why_moved", "args": {"query": "zzzz qqqq", "on": None}})
    return calls


def capture(actions=(), nse=None, news=None, calls=None):
    store = build_store(HISTORY, actions=actions)
    return capture_snapshot(store, INSTRUMENTS, nse or FULL_NSE(), news or FULL_NEWS(), NOW, calls or scenarios())


def replayed(snap):
    asm, _now = replay_assembler(snap)
    return {r["id"]: jsonable(run_call(asm, r["call"])) for r in snap["results"]}


# ---------------------------------------------------------------- JSON form
def test_dataclasses_tuples_and_dicts_survive_a_json_round_trip():
    items = [NewsResult("ok", "q", (NewsItem("t", "Mint", "2026-11-09T10:00:00+05:30", "https://n/1"),), "x", 3,
                        {"stale": 2}, "note"),
             Announcement("ACME", "Acme", "2026-11-09T10:00:00", "Outcome", "text", None),
             FiiDii("2026-11-09", "DII", 1.5, 1.0, 0.5), StoredAction("2026-11-06", "bonus", True, "Bonus 1:1", None),
             [FiiDii("2026-11-09", "FII/FPI", 2.0, 3.0, -1.0)], None, 7, "s"]
    for obj in items:
        assert decode(json.loads(json.dumps(encode(obj)))) == obj
    news = decode(json.loads(json.dumps(encode(items[0]))))
    assert isinstance(news.items, tuple) and news.dropped == {"stale": 2}


def test_call_keys_ignore_nothing_that_matters():
    assert call_key("announcements", ("ACME", "2026-10-01", "2026-11-09"), {}) != call_key(
        "announcements", ("ACME", "2026-10-02", "2026-11-09"), {})
    assert call_key("fetch", ("n",), {"a": 1, "b": 2}) == call_key("fetch", ("n",), {"b": 2, "a": 1})


# ---------------------------------------------------------------- recorder and replay
def test_recorder_keeps_results_and_errors_and_replay_gives_them_back():
    rec = Recorder(FakeNse(flows=[FiiDii("2026-11-09", "DII", 5.0, 3.0, 2.0)], fail=("announcements",)))
    assert rec.fii_dii()[0].net == 2.0
    with pytest.raises(NseError):
        rec.announcements("ACME", "a", "b")
    replay = Replay(rec.calls)
    assert replay.fii_dii()[0].net == 2.0
    with pytest.raises(NseError, match="HTTP 500"):
        replay.announcements("ACME", "a", "b")


def test_replay_refuses_a_call_that_was_never_recorded():
    replay = Replay(Recorder(FakeNse()).calls)
    with pytest.raises(KeyError, match="unrecorded call"):
        replay.announcements("ACME", "2026-10-01", "2026-11-09")
    with pytest.raises(AttributeError):
        replay._private


# ---------------------------------------------------------------- capture then replay
@pytest.mark.parametrize("actions", [(), (DIVIDEND,), (BONUS,), (DIVIDEND, BONUS)])
def test_replayed_scenarios_give_exactly_the_recorded_packets(actions):
    snap = capture(actions)
    stored = {r["id"]: r["packet"] for r in snap["results"]}
    assert replayed(snap) == stored
    assert len(stored) == len(scenarios()) == 6


def test_the_recording_really_covers_the_cases(tmp_path):
    snap = capture((DIVIDEND, BONUS))
    by_id = {r["id"]: r["packet"] for r in snap["results"]}
    assert by_id["why:acme:2026-11-09"]["status"] == "ok" and by_id["why:acme:2026-11-09"]["context"]
    assert any(f["kind"] == "news" for f in by_id["why:acme:2026-11-09"]["factors"])        # news was recorded and replayed
    assert {f["label"] for f in by_id["why:acme:2026-11-06"]["facts"]} >= {"Previous close (unadjusted)"}
    assert by_id["return:acme:1w"]["status"] == "refused"                                  # the bonus is inside the week
    assert by_id["why:nonsense"]["status"] == "clarify"
    assert snap["symbols"] == ["ACME"] and set(snap["full_days"]) == {"2026-11-06", "2026-11-09"}
    assert snap["latest_day"] == "2026-11-09" and snap["days"] == sorted(HISTORY)
    path = tmp_path / "s.json.gz"
    save_snapshot(snap, path)
    assert load_snapshot(path) == json.loads(json.dumps(snap)) and path.stat().st_size < 60_000


def test_a_dividend_return_replays_with_its_dividend_fact():
    snap = capture((DIVIDEND,))
    packet = replayed(snap)["return:acme:1w"]
    assert packet["status"] == "ok" and any(f["label"].startswith("Dividend, ex-date") for f in packet["facts"])


def test_failed_sources_are_recorded_as_failures_and_replayed_as_failures():
    snap = capture((), nse=FakeNse(fail=("announcements", "board_meetings", "next_results", "fii_dii")),
                   news=FakeNews(status="error"))
    packets = replayed(snap)
    gaps = {g["code"] for g in packets["why:acme:2026-11-09"]["gaps"]}
    assert {"announcements_unavailable", "board_meetings_unavailable", "news_unavailable"} <= gaps
    assert packets == {r["id"]: r["packet"] for r in snap["results"]}
    assert any(rec.get("error") for rec in snap["nse_calls"].values())


def test_replay_store_keeps_days_with_no_captured_stock_and_the_full_market_only_on_scenario_days():
    snap = capture()
    asm, _ = replay_assembler(snap)
    store = asm.store
    assert store.all_days() == sorted(HISTORY) and store.latest_day() == "2026-11-09"
    assert len(store.get_day("2026-11-09")) == 251 and len(store.get_day("2026-11-05")) == 1   # ACME + 250 fillers; ACME only
    snap["prices"]["ACME"].pop("2026-11-05")                                               # a day the stock has no row
    asm2, _ = replay_assembler(snap)
    assert "2026-11-05" in asm2.store.all_days() and asm2.store.get_day("2026-11-05") == {}


def test_changed_behaviour_is_caught_not_hidden():
    snap = capture()
    asm, _ = replay_assembler(snap)
    drifted = jsonable(run_call(asm, snap["results"][0]["call"]))
    drifted["facts"][0]["value"] = "Rs 999.00"
    assert drifted != snap["results"][0]["packet"]
    other_day = {"id": "x", "fn": "why_moved", "args": {"query": "acme", "on": "2026-11-05"}}
    with pytest.raises(KeyError, match="unrecorded call"):                                  # code asking for something new
        run_call(asm, other_day)


# ---------------------------------------------------------------- scenarios and summary
def test_build_calls_lists_every_scenario_once():
    calls = build_calls(["reliance", "tcs"], ["2026-11-03", "2026-11-02", "2026-10-30"])
    assert len(calls) == 2 * (3 + len(PERIODS) + 1) == 20
    assert len({c["id"] for c in calls}) == 20 and "why:tcs:2026-10-30" in {c["id"] for c in calls}
    assert calls[0] == {"id": "why:reliance:2026-11-03", "fn": "why_moved", "args": {"query": "reliance", "on": "2026-11-03"}}


def test_summary_has_a_readable_line_per_scenario():
    snap = capture((DIVIDEND,))
    lines = summarize(snap["results"]).splitlines()
    assert len(lines) == 6
    first = next(l for l in lines if l.startswith("why:acme:2026-11-09"))
    assert "Official close, 9 Nov 2026: ₹110.00" in first and "[move/ok] ACME" in first and "gaps:" in first
