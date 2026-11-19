"""Regression tests from REAL data: the Phase 1 stocks on the Phase 1 days, recorded once from NSE with
`capture-scenarios` and replayed offline (no network, fixed clock). Three layers:

  1. golden   - every scenario must reproduce the recorded packet exactly;
  2. arithmetic - closes, changes, returns and breadth are recomputed here from the raw recorded prices;
  3. policy   - the rules that protect the user (no look-ahead, honest gaps, listing history, ETFs, validators).

If a change is MEANT to alter an answer, read the diff, then accept it:
    python -m briany.snapshot tests/fixtures/phase1/phase1_scenarios.json.gz
"""
import re
import statistics
from datetime import date as Date
from pathlib import Path

import pytest

import briany.evidence as ev
from briany.answer import MAX_LINES, build_footer, fallback_text, validate_text
from briany.snapshot import jsonable, load_snapshot, replay_assembler, run_call
from briany.symbols import is_entitlement

FIXTURE = Path(__file__).parent / "fixtures" / "phase1" / "phase1_scenarios.json.gz"
SNAP = load_snapshot(FIXTURE)
ASM, NOW = replay_assembler(SNAP)
STORED = {r["id"]: r["packet"] for r in SNAP["results"]}
PACKETS = {r["id"]: run_call(ASM, r["call"]) for r in SNAP["results"]}
PRICES = SNAP["prices"]                                        # {symbol: {day: [close, previous close, volume]}}
SYMBOL = {"reliance": "RELIANCE", "tcs": "TCS", "infosys": "INFY", "tempsens": "TEMPSENS", "krn": "KRN",
          "goldbees": "GOLDBEES"}
WHY_DAYS = ["2026-11-03", "2026-11-02", "2026-10-30"]
LATEST = "2026-11-18"
# Hand-checked calendar for a 18 Nov 2026 end: a month back is Sun 18 Oct, so the last close before it is Fri 16 Oct.
STARTS = {"1w": "2026-11-11", "1m": "2026-10-16", "3m": "2026-08-18", "6m": "2026-05-18", "1y": "2025-11-18",
          "ytd": "2025-12-31"}
RETURN_IDS = [f"return:{q}:{p}" for q in SYMBOL for p in STARTS]
WHY_IDS = [f"why:{q}:{d}" for q in SYMBOL for d in WHY_DAYS]
EVENT_IDS = [f"events:{q}" for q in SYMBOL]


def money(x):
    return f"₹{x:,.2f}"


def human(day):
    d = Date.fromisoformat(day)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def pct(new, old):
    return f"{round((new - old) / old * 100, 2):+.2f}%"


def facts(id_):
    return {f["label"]: f["value"] for f in STORED[id_]["facts"]}


# ---------------------------------------------------------------- the recording itself
def test_the_fixture_holds_every_phase1_scenario_from_real_data():
    assert len(STORED) == 60 and set(STORED) == set(WHY_IDS) | set(RETURN_IDS) | set(EVENT_IDS)
    assert SNAP["latest_day"] == LATEST and sorted(SNAP["symbols"]) == sorted(SYMBOL.values())
    assert SNAP["captured_at"].startswith("2026-11-19") and set(SNAP["full_days"]) == set(WHY_DAYS)
    assert not any(v.get("error") for v in SNAP["nse_calls"].values())              # no source failed during the recording
    assert SNAP["news_calls"] == {}                                                  # no scraped headlines are kept


def test_the_recorded_prices_are_continuous():
    """Each row's previous close equals the stock's previous stored close: no unadjusted break in these six histories."""
    for sym, per_day in PRICES.items():
        days = sorted(per_day)
        for a, b in zip(days, days[1:]):
            assert per_day[b][1] == pytest.approx(per_day[a][0], abs=0.011), (sym, a, b)


# ---------------------------------------------------------------- 1. golden
@pytest.mark.parametrize("id_", sorted(STORED))
def test_golden_packet(id_):
    assert jsonable(PACKETS[id_]) == STORED[id_], (
        f"{id_} changed. If that is intended, read the diff and accept it with: "
        "python -m briany.snapshot tests/fixtures/phase1/phase1_scenarios.json.gz")


def test_replay_is_strict_about_what_it_was_never_asked():
    with pytest.raises(KeyError, match="unrecorded call"):
        ASM.why_moved("reliance", on="2026-11-04")


# ---------------------------------------------------------------- 2. arithmetic from the raw prices
@pytest.mark.parametrize("id_", WHY_IDS)
def test_why_moved_numbers_match_the_raw_prices(id_):
    _, query, day = id_.split(":")
    close, prev, _vol = PRICES[SYMBOL[query]][day]
    f = facts(id_)
    assert f[f"Official close, {human(day)}"] == money(close)
    assert f["Previous close"] == money(prev)
    assert f["Change from previous close"] == pct(close, prev)


@pytest.mark.parametrize("day", WHY_DAYS)
def test_market_context_matches_a_recount_of_the_whole_day(day):
    from briany.store import unpack_day
    changes = []
    for sym, row in unpack_day(SNAP["full_days"][day]).items():
        if not row.prev_close or row.prev_close < ev.MIN_JUMP_PRICE or is_entitlement(sym):
            continue
        change = (row.close - row.prev_close) / row.prev_close * 100
        if abs(change) < ev.JUMP_PCT:
            changes.append(change)
    expected = {f"Median change across {len(changes)} listed stocks": f"{round(statistics.median(changes), 2):+.2f}%",
                "Stocks up / down": f"{sum(c > 0 for c in changes)} / {sum(c < 0 for c in changes)}"}
    for q in SYMBOL:
        got = {f["label"]: f["value"] for f in STORED[f"why:{q}:{day}"]["context"]}
        assert got == expected


@pytest.mark.parametrize("id_", RETURN_IDS)
def test_returns_use_the_checked_dates_and_match_the_raw_prices(id_):
    _, query, label = id_.split(":")
    sym, packet = SYMBOL[query], STORED[id_]
    start = STARTS[label]
    if start not in PRICES[sym]:                                                     # listed after the start date
        assert packet["status"] == "refused" and sym == "TEMPSENS"
        return
    assert packet["status"] == "ok"
    s_close, e_close = PRICES[sym][start][0], PRICES[sym][LATEST][0]
    f = facts(id_)
    assert f[f"Official close, {human(start)}"] == money(s_close)
    assert f[f"Official close, {human(LATEST)}"] == money(e_close)
    assert f["Price return"] == pct(e_close, s_close)


@pytest.mark.parametrize("id_", [i for i in RETURN_IDS if STORED[i]["status"] == "ok"])
def test_every_dividend_in_the_period_is_listed_and_none_outside_it(id_):
    _, query, label = id_.split(":")
    sym, start = SYMBOL[query], STARTS[label]
    expected = sorted(a["f"]["ex_date"] for a in SNAP["actions"][sym]
                      if a["f"]["kind"] == "dividend" and start < a["f"]["ex_date"] <= LATEST)
    shown = sorted(re.search(r"ex-date (\d+ \w+ \d{4})", f["label"]).group(1)
                   for f in STORED[id_]["facts"] if f["label"].startswith("Dividend, ex-date"))
    assert shown == sorted(human(d) for d in expected)


# ---------------------------------------------------------------- a few numbers to confirm against a broker
KEY_NUMBERS = [
    ("why:reliance:2026-11-02", {"Official close, 2 Nov 2026": "₹1,300.00", "Previous close": "₹1,164.90",
                                 "Change from previous close": "+11.60%"}),
    ("why:tcs:2026-11-03", {"Official close, 3 Nov 2026": "₹2,499.10", "Change from previous close": "-0.10%"}),
    ("why:krn:2026-11-03", {"Official close, 3 Nov 2026": "₹1,035.50", "Change from previous close": "-4.99%"}),
    ("why:tempsens:2026-11-03", {"Official close, 3 Nov 2026": "₹552.25", "Change from previous close": "+13.54%"}),
    ("return:tcs:1y", {"Price return": "-22.92%"}),
    ("return:goldbees:1y", {"Price return": "+6.88%"}),
]


@pytest.mark.parametrize("id_,expected", KEY_NUMBERS)
def test_key_numbers(id_, expected):
    f = facts(id_)
    for label, value in expected.items():
        assert f[label] == value


# ---------------------------------------------------------------- 3. policy on real packets
@pytest.mark.parametrize("id_", WHY_IDS)
def test_no_factor_is_dated_after_the_day_that_moved(id_):
    day = id_.rsplit(":", 1)[1]
    for f in STORED[id_]["factors"]:
        assert f["when"][:10] <= day, (id_, f["when"], f["title"])


@pytest.mark.parametrize("id_", WHY_IDS)
def test_old_days_say_news_was_not_checked_and_never_claim_nothing_was_found(id_):
    p, text = STORED[id_], fallback_text(PACKETS[id_])
    assert any(g["code"] == "news_not_covered" for g in p["gaps"])
    assert p["evidence_complete"] is False and p["no_clear_evidence"] is False
    assert "news for this date was not checked" in text and "no clear" not in text.lower()


def test_reliance_after_its_results_meeting_shows_the_filings_and_the_meeting():
    p = STORED["why:reliance:2026-11-02"]
    kinds = [f["kind"] for f in p["factors"]]
    assert kinds.count("board_meeting") == 1 and kinds.count("announcement") == 5
    assert any(f["title"] == "Outcome of Board Meeting" for f in p["factors"])


def test_an_ex_dividend_day_is_listed_as_a_possible_factor_not_a_cause():
    text = fallback_text(PACKETS["why:infosys:2026-11-03"])
    assert "Interim Dividend - Rs 26 Per Share" in text and "not a confirmed cause" in text
    assert any("Dividend, ex-date 3 Nov 2026" in f["label"] and f["value"] == "₹26.00" for f in STORED["return:infosys:1m"]["facts"])


def test_krn_three_lower_circuit_days_are_reported_exactly_and_without_a_cause():
    for d in WHY_DAYS:
        assert facts(f"why:krn:{d}")["Change from previous close"] == "-4.99%"


def test_tempsens_listed_in_august_so_long_periods_are_refused_with_a_clear_reason():
    assert min(PRICES["TEMPSENS"]) == "2026-08-28"
    for label in ("3m", "6m", "1y", "ytd"):
        p = STORED[f"return:tempsens:{label}"]
        assert p["status"] == "refused" and not p["facts"]
        assert p["gaps"][0]["code"] == "no_price_data" and "listed after that date" in p["gaps"][0]["text"]
        text = fallback_text(PACKETS[f"return:tempsens:{label}"])
        assert "can't give a reliable figure" in text and "not in the" not in text and "Price return" not in text
    for label in ("1w", "1m"):
        assert STORED[f"return:tempsens:{label}"]["status"] == "ok"


def test_the_etf_has_prices_and_returns_but_no_company_filings():
    for d in WHY_DAYS:
        assert STORED[f"why:goldbees:{d}"]["status"] == "ok" and STORED[f"why:goldbees:{d}"]["factors"] == []
    assert STORED["events:goldbees"]["factors"] == []
    assert all(STORED[f"return:goldbees:{p}"]["status"] == "ok" for p in STARTS)


@pytest.mark.parametrize("q,count", [("reliance", 6), ("tcs", 4), ("infosys", 4), ("tempsens", 3), ("krn", 0), ("goldbees", 0)])
def test_recent_events_per_stock(q, count):
    p = STORED[f"events:{q}"]
    assert len(p["factors"]) == count and p["status"] == "ok"
    assert [g["code"] for g in p["gaps"]] == ["next_results_not_announced"]


@pytest.mark.parametrize("id_", sorted(STORED))
def test_the_code_built_answer_is_valid_clean_and_short_for_every_real_packet(id_):
    packet = PACKETS[id_]
    text = fallback_text(packet)
    assert validate_text(text, packet) == []
    assert len(text.splitlines()) <= MAX_LINES
    footer = build_footer(packet, html_links=True)
    assert "Information only, not investment advice." in footer
    for part in (text, footer):
        assert not re.search(r"\bNone\b|\bnan\b|\{|\}|\bTODO\b", part), part
    assert all(url.startswith("https://") for url in re.findall(r'href="([^"]+)"', footer))
