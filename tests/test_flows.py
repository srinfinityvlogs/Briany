# SPDX-License-Identifier: Apache-2.0
"""Daily FII/DII and deal snapshots. Fixtures are the real NSE responses of 20 Nov 2026 (public data)."""
import copy
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest

from briany.flows import IST, apply_updates, build_updates, flows_report, ingest_flows, prune
from briany.nse import NseError
from briany.store import MemoryStore, RedisStore

FIX = Path(__file__).parent / "fixtures" / "flows"
FII = json.loads((FIX / "fii_dii_20112026.json").read_text())
DEALS = json.loads((FIX / "largedeal_20112026.json").read_text())
NOW = datetime(2026, 11, 21, 21, 30, tzinfo=IST)


@pytest.fixture(params=["memory", "redis"])
def store(request):
    return MemoryStore() if request.param == "memory" else RedisStore(fakeredis.FakeRedis(decode_responses=True))


class FakeNse:
    def __init__(self, fii=FII, deals=DEALS, fail=()):
        self.fii, self.deals, self.fail, self.calls = fii, deals, set(fail), []

    def get_json(self, path, params=None, ttl=0):
        self.calls.append(path)
        if path in self.fail:
            raise NseError(f"HTTP 503 from NSE ({path})")
        return self.fii if path == "fiidiiTradeReact" else self.deals


# ================================================================ building records from the real feeds
def test_real_feeds_become_records_filed_under_each_kinds_own_date():
    updates, problems, notes = build_updates(FII, DEALS)
    assert problems == [] and sorted(updates) == ["2026-11-19", "2026-11-20"]
    day = updates["2026-11-20"]
    assert [r[0] for r in day["fii"]] == ["DII", "FII/FPI"] and day["fii"][0] == ["DII", 18492.63, 23352.43, -4859.8]
    assert len(day["bulk"]) == 108 and len(day["block"]) == 5 and day["declared"] == {"bulk": 108, "block": 5}
    assert day["bulk"][0] == ["SMCGLOBAL", "ARIHANT CAPITAL MARKETS LIMITED", "BUY", 2282076, 86.69, ""]   # '-' remark -> ''
    assert day["block"][0][:5] == ["GALLANTT", "SHREE SURABHI FLOUR MILLS PRIVATE LIMITED", "BUY", 550000, 523.0]
    assert "short" not in day
    short = updates["2026-11-19"]                                    # short-selling rows are dated one day earlier
    assert len(short["short"]) == 84 and short["short"][0] == ["360ONE", 7] and short["declared"] == {"short": 84}


def test_a_kind_whose_listed_rows_differ_from_nses_declared_count_is_not_stored():
    deals = copy.deepcopy(DEALS)
    deals["BULK_DEALS"] = "109"
    updates, problems, _ = build_updates(FII, deals)
    assert "bulk" not in updates["2026-11-20"] and len(updates["2026-11-20"]["block"]) == 5     # the others still are
    assert problems == ["large deals bulk: NSE declares 109 but 108 usable rows are listed; not stored"]
    deals = copy.deepcopy(DEALS)
    deals["BLOCK_DEALS_DATA"].append({"buySell": "BUY", "clientName": "X", "date": "20-Nov-2026", "name": "N", "qty": "bad",
                                      "remarks": None, "symbol": "ZZZ", "watp": "1"})       # an unreadable row is not silently dropped
    deals["BLOCK_DEALS"] = "6"
    assert "block" not in build_updates(FII, deals)[0]["2026-11-20"]


def test_missing_or_odd_declared_counts_and_mixed_days_are_refused():
    deals = copy.deepcopy(DEALS)
    del deals["SHORT_DEALS"]
    assert any("short: no declared count" in p for p in build_updates(FII, deals)[1])
    deals = copy.deepcopy(DEALS)
    deals["BULK_DEALS_DATA"][3]["date"] = "19-Nov-2026"
    updates, problems, _ = build_updates(FII, deals)
    assert "bulk" not in updates.get("2026-11-20", {}) and any("rows span several days" in p for p in problems)


def test_fii_dii_needs_both_categories():
    one = [r for r in FII if r["category"] == "DII"]
    updates, problems, _ = build_updates(one, None)
    assert updates == {} and "not both of ['DII', 'FII/FPI']" in problems[0]
    assert build_updates([], None)[1] == ["FII/DII: no rows in the response"]
    assert "unexpected response" in build_updates({"not": "a list"}, None)[1][0]
    assert "unexpected response" in build_updates(None, [1, 2])[1][0]


def test_an_empty_bulk_or_block_list_is_stored_as_empty_but_an_empty_short_list_has_no_date():
    deals = copy.deepcopy(DEALS)
    deals.update({"BLOCK_DEALS": "0", "BLOCK_DEALS_DATA": [], "SHORT_DEALS": "0", "SHORT_DEALS_DATA": []})
    updates, problems, notes = build_updates(None, deals)
    assert problems == [] and updates["2026-11-20"]["block"] == [] and "2026-11-19" not in updates
    assert any("short: none declared" in n for n in notes)


# ================================================================ storing (memory and fake Redis)
def test_ingest_stores_then_a_second_run_changes_nothing(store):
    lines, errors = ingest_flows(FakeNse(), store, now=NOW)
    assert errors == 0 and store.flow_days() == ["2026-11-19", "2026-11-20"]
    assert "rewritten: 2026-11-19, 2026-11-20" in lines
    first = store.get_flow("2026-11-20")
    assert first["stored"] == "2026-11-21T21:30:00+05:30" and len(first["bulk"]) == 108
    lines, errors = ingest_flows(FakeNse(), store, now=datetime(2026, 11, 22, 3, 30, tzinfo=IST))
    assert errors == 0 and "rewritten: nothing (already stored, unchanged)" in lines
    assert store.get_flow("2026-11-20")["stored"] == first["stored"]                    # not even the timestamp moved


def test_a_new_day_is_added_and_older_days_are_untouched(store):
    ingest_flows(FakeNse(), store, now=NOW)
    before = store.get_flow("2026-11-20")
    fii, deals = copy.deepcopy(FII), copy.deepcopy(DEALS)
    for r in fii:
        r["date"] = "23-Nov-2026"
    deals["as_on_date"] = "23-Nov-2026"
    for key in ("BULK_DEALS_DATA", "BLOCK_DEALS_DATA"):
        for r in deals[key]:
            r["date"] = "23-Nov-2026"
    for r in deals["SHORT_DEALS_DATA"]:
        r["date"] = "20-Nov-2026"
    ingest_flows(FakeNse(fii, deals), store, now=datetime(2026, 11, 23, 21, 30, tzinfo=IST))
    assert store.flow_days() == ["2026-11-19", "2026-11-20", "2026-11-23"]
    assert store.get_flow("2026-11-23")["fii"][0][0] == "DII"
    merged = store.get_flow("2026-11-20")                                                # short rows for 20 Nov merged in
    assert len(merged["short"]) == 84 and merged["bulk"] == before["bulk"] and merged["fii"] == before["fii"]


def test_late_short_rows_merge_into_a_day_that_already_has_its_other_data(store):
    store.put_flow("2026-11-19", {"fii": [["DII", 1.0, 2.0, -1.0], ["FII/FPI", 3.0, 2.0, 1.0]], "bulk": [["AAA", "C", "BUY", 5, 9.0, ""]],
                                  "declared": {"bulk": 1}, "stored": "x"})
    apply_updates(store, build_updates(None, DEALS)[0], NOW)
    rec = store.get_flow("2026-11-19")
    assert len(rec["short"]) == 84 and rec["bulk"] == [["AAA", "C", "BUY", 5, 9.0, ""]] and rec["declared"] == {"bulk": 1, "short": 84}
    assert rec["fii"][0] == ["DII", 1.0, 2.0, -1.0]


def test_one_feed_failing_does_not_stop_the_other_and_is_reported(store):
    lines, errors = ingest_flows(FakeNse(fail={"fiidiiTradeReact"}), store, now=NOW)
    assert errors == 1 and any(l.startswith("ERROR FII/DII: HTTP 503") for l in lines)
    assert "fii" not in store.get_flow("2026-11-20") and len(store.get_flow("2026-11-20")["bulk"]) == 108
    lines, errors = ingest_flows(FakeNse(fail={"fiidiiTradeReact", "snapshot-capital-market-largedeal"}), MemoryStore(), now=NOW)
    assert errors == 2 and lines[-1] == "rewritten: nothing (already stored, unchanged)"


def test_an_incomplete_kind_is_a_visible_error_but_the_rest_is_kept(store):
    deals = copy.deepcopy(DEALS)
    deals["BULK_DEALS"] = "200"
    lines, errors = ingest_flows(FakeNse(deals=deals), store, now=NOW)
    assert errors == 1 and any(l.startswith("WARN large deals bulk") for l in lines)
    rec = store.get_flow("2026-11-20")
    assert "bulk" not in rec and len(rec["block"]) == 5 and len(rec["fii"]) == 2


def test_old_days_are_pruned_from_the_newest_stored_day(store):
    for d in ("2025-09-01", "2025-10-15", "2025-10-16", "2026-11-20"):
        store.put_flow(d, {"fii": []})
    assert prune(store) == ["2025-09-01", "2025-10-15"]       # newest 2026-11-20 minus 400 days = 2025-10-16, kept
    assert store.flow_days() == ["2025-10-16", "2026-11-20"] and store.get_flow("2025-10-15") is None
    assert prune(MemoryStore()) == []


def test_a_real_day_is_small_enough_for_the_redis_budget(store):
    ingest_flows(FakeNse(), store, now=NOW)
    size = store.flow_bytes("2026-11-20") + store.flow_bytes("2026-11-19")
    assert 1000 < size < 12000                              # compressed; about 255 days of this is well under 3 MB
    report = "\n".join(flows_report(store))
    assert "2 day record(s) stored, 2026-11-19 .. 2026-11-20" in report
    assert "2026-11-20: fii 2 rows, bulk 108, block 5, short -" in report and "2026-11-19: fii -, bulk -, block -, short 84" in report
    assert flows_report(MemoryStore()) == ["no FII/DII or deal records stored yet"]


# ================================================================ the command line
def test_cli_stores_checks_and_exits_red_on_errors(monkeypatch, capsys):
    from briany import flows_ingest
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("briany.ingest.connect", lambda: (redis, RedisStore(redis), SimpleNamespace(redis_max_memory_mb=30)))
    nse = FakeNse()
    monkeypatch.setattr("briany.nse.NseClient", lambda **k: nse)
    with pytest.raises(SystemExit) as e:
        flows_ingest.main([])
    assert e.value.code == 0 and RedisStore(redis).flow_days() == ["2026-11-19", "2026-11-20"]
    out = capsys.readouterr().out
    assert "ok   large deals bulk 2026-11-20: 108 rows (declared 108)" in out and "2 day record(s) stored" in out
    flows_ingest.main(["--check"])
    assert "stored size:" in capsys.readouterr().out
    nse.fail = {"fiidiiTradeReact"}
    with pytest.raises(SystemExit) as e:
        flows_ingest.main([])
    assert e.value.code == 1
