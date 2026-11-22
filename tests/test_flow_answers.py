# SPDX-License-Identifier: Apache-2.0
"""Feature 3b: FII/DII and bulk/block/short-deal answers from stored daily records. The 20 Nov 2026 records come from the real NSE
feeds (tests/fixtures/flows); other days are synthetic with round numbers so the sums can be checked by hand."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from briany.answer import build_footer, fallback_text, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler
from briany.flows import apply_updates, build_updates
from briany.marketdata import PriceRow
from briany.router import Context, RouteError, validate_route
from briany.store import MemoryStore
from briany.symbols import Instrument, Resolver
from test_evidence import FakeNews, FakeNse
from test_index_close import DeadChain

FIX = Path(__file__).parent / "fixtures" / "flows"
FII = json.loads((FIX / "fii_dii_20112026.json").read_text())
DEALS = json.loads((FIX / "largedeal_20112026.json").read_text())
REAL, _p, _n = build_updates(FII, DEALS)
BULK, BLOCK, SHORT = REAL["2026-11-20"]["bulk"], REAL["2026-11-20"]["block"], REAL["2026-11-19"]["short"]
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=IST)
SYMBOLS = sorted({r[0] for r in BULK + BLOCK + SHORT} | {"TCS", "INFY"})
RESOLVER = Resolver([Instrument(s, f"{s} LIMITED") for s in SYMBOLS])
PRICE_DAYS = ("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20")


def fii_rows(buy_f=1100.0, sell_f=1000.0, buy_d=950.0, sell_d=1000.0):
    return [["DII", buy_d, sell_d, round(buy_d - sell_d, 2)], ["FII/FPI", buy_f, sell_f, round(buy_f - sell_f, 2)]]


def make_store(flow_days=("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"), real=True, prices=PRICE_DAYS):
    s = MemoryStore()
    for d in prices:
        s.put_day(d, {"TCS": PriceRow(100.0, 100.0, 10)})
    s.set_actions([], {"updated": "2026-11-20", "start": "2025-10-06", "end": "2027-01-09", "count": 0, "blocking": 0, "symbols": 0})
    for d in flow_days:
        s.put_flow(d, {"fii": fii_rows()})
    if real:
        apply_updates(s, REAL, NOW)                          # 20 Nov real; 19 Nov gains the real short rows, keeps its fii
    return s


def asm(store=None, now=NOW):
    return Assembler(store or make_store(), FakeNse(), FakeNews(), RESOLVER, now=lambda: now)


# ================================================================ FII/DII for one day
def test_latest_day_from_the_real_feed():
    p = asm().fii_dii()
    assert (p.kind, p.status, p.name) == ("flows", "ok", "FII/DII") and p.gaps == ()
    text = fallback_text(p)
    assert text.splitlines() == [
        "FII/FPI on 20 Nov 2026: bought ₹23,596.40 crore, sold ₹13,287.98 crore, net +₹10,308.42 crore (net buying).",
        "DII on 20 Nov 2026: bought ₹18,492.63 crore, sold ₹23,352.43 crore, net -₹4,859.80 crore (net selling)."]
    assert validate_text(text, p) == []
    footer = build_footer(p)
    assert footer.startswith("Sources: NSE FII/DII data") and "not live" not in footer


def test_a_named_stored_day_and_missing_days_say_what_exists():
    p = asm().fii_dii(on="2026-11-18")
    assert fallback_text(p).splitlines()[0] == ("FII/FPI on 18 Nov 2026: bought ₹1,100.00 crore, sold ₹1,000.00 crore, net "
                                                "+₹100.00 crore (net buying).")
    for day in ("2026-11-14", "2026-11-12"):                          # a Saturday, and a day before storage began
        q = asm().fii_dii(on=day)
        assert q.status == "refused" and q.gaps[0].code == "no_flow_record" and "records start on 16 Nov 2026" in q.gaps[0].text
        assert fallback_text(q).startswith("I can't answer that from the stored data.")
    assert asm().fii_dii(on="banana").gaps[0].code == "bad_date"


def test_when_the_latest_flows_are_not_stored_yet_the_older_day_is_labelled():
    s = make_store(real=False, prices=PRICE_DAYS)                       # prices to 20 Nov, flows only to 19 Nov
    p = asm(s).fii_dii()
    assert p.status == "ok" and [g.code for g in p.gaps] == ["flows_not_yet"]
    text = fallback_text(p)
    assert text.startswith("FII/FPI on 19 Nov 2026") and "for 20 Nov 2026 is not stored yet" in text
    assert validate_text(text, p) == []


def test_nothing_stored_and_a_day_without_fii_data():
    empty = MemoryStore()
    assert asm(empty).fii_dii().gaps[0].code == "no_flow_data" and asm(empty).large_deals().gaps[0].code == "no_flow_data"
    s = make_store(flow_days=(), real=False)
    s.put_flow("2026-11-19", {"short": [["AAA", 5]]})                  # a record with no FII/DII in it
    assert asm(s).fii_dii().gaps[0].code == "no_flow_data"
    assert asm(s).fii_dii(on="2026-11-19").gaps[0].code == "no_flow_record"


def test_rows_that_do_not_add_up_are_refused():
    s = make_store(real=False)
    s.put_flow("2026-11-19", {"fii": [["DII", 950.0, 1000.0, -50.0], ["FII/FPI", 1100.0, 1000.0, 250.0]]})   # net should be 100
    p = asm(s).fii_dii(on="2026-11-19")
    assert p.status == "refused" and p.gaps[0].code == "flows_inconsistent" and "FII/FPI bought minus sold" in p.gaps[0].text
    s.put_flow("2026-11-19", {"fii": [["DII", 950.0, 1000.0, -50.0]]})
    assert "FII/FPI is missing" in asm(s).fii_dii(on="2026-11-19").gaps[0].text


# ================================================================ FII/DII over a period
def test_a_complete_period_is_added_up_and_checked_by_hand():
    p = asm().fii_dii(label="1w")                                       # trading days after 13 Nov, to 20 Nov: 16..20
    assert (p.kind, p.status) == ("flows_period", "ok")
    text = fallback_text(p)
    assert text.splitlines() == [
        "FII/DII added up over 5 trading days, 16 Nov 2026 to 20 Nov 2026.",
        "FII/FPI: bought ₹27,996.40 crore, sold ₹17,287.98 crore, net +₹10,708.42 crore (net buying).",
        "DII: bought ₹22,292.63 crore, sold ₹27,352.43 crore, net -₹5,059.80 crore (net selling).",
        "Totals are the daily NSE figures added up here."]
    assert validate_text(text, p) == []


def test_a_period_with_missing_days_is_not_totalled_but_the_stored_days_are_shown():
    s = make_store(flow_days=("2026-11-16", "2026-11-19"))              # 17 and 18 Nov missing
    p = asm(s).fii_dii(label="1w")
    assert (p.kind, p.status) == ("flows_period", "refused") and p.gaps[0].code == "flows_partial"
    text = fallback_text(p)
    assert text.splitlines()[0] == ("I have FII/DII data for only 3 of the 5 trading days from 16 Nov 2026 to 20 Nov 2026, "
                                    "so I am not giving a total.")
    assert "16 Nov 2026: FII/FPI net +₹100.00 crore (net buying); DII net -₹50.00 crore (net selling)." in text
    assert "20 Nov 2026: FII/FPI net +₹10,308.42 crore (net buying)" in text and "added up" not in text
    assert validate_text(text, p) == []
    only_one = asm(make_store(flow_days=())).fii_dii(label="1w")        # just the real 20 Nov
    assert only_one.gaps[0].text.startswith("I have FII/DII data for only 1 of the 5 trading days")


def test_period_edges():
    assert asm(make_store(flow_days=(), real=False)).fii_dii(label="1w").gaps[0].code == "no_flow_data"
    s = make_store(flow_days=(), real=False)
    s.put_flow("2026-11-02", {"fii": fii_rows()})                       # stored, but outside the week
    assert asm(s).fii_dii(label="1w").gaps[0].code == "no_flow_record"
    assert asm().fii_dii(label="1m").gaps[0].code == "insufficient_history"    # prices only reach back to 13 Nov
    assert asm().fii_dii(label="banana").gaps[0].code == "bad_period"
    bad = make_store()
    bad.put_flow("2026-11-17", {"fii": [["DII", 1.0, 2.0, 9.0], ["FII/FPI", 1100.0, 1000.0, 100.0]]})
    assert asm(bad).fii_dii(label="1w").gaps[0].code == "flows_inconsistent"


# ================================================================ deals for one stock
def test_one_stocks_deals_across_stored_days():
    p = asm().large_deals(query="GALLANTT")
    assert (p.kind, p.status, p.symbol) == ("deals", "ok", "GALLANTT")
    facts = {f.label: f.value for f in p.facts}
    n = sum(1 for r in BULK + BLOCK if r[0] == "GALLANTT")               # counted straight from the raw rows
    assert n == 4 and p.facts[0].raw == n and p.facts[0].value == f"{n} in 2 stored day(s), 19 Nov 2026 to 20 Nov 2026"
    text = fallback_text(p)
    assert "Bulk deal, 20 Nov 2026: GALLANTT: SELL 1,610,292 shares at ₹523.93, client DINESH R AGARWAL." in text
    assert "Block deal, 20 Nov 2026: GALLANTT: BUY 550,000 shares at ₹523.00, client SHREE SURABHI FLOUR MILLS PRIVATE LIMITED." in text
    assert "Not every stored day has every kind of deal record" in text          # 19 Nov has short rows only
    assert validate_text(text, p) == [] and facts


def test_a_stock_with_many_deals_shows_the_latest_few_and_says_how_many():
    p = asm().large_deals(query="UGROCAP", kind="bulk")
    assert p.facts[0].raw == 20 and "the latest 6 are listed" in p.facts[0].value and len(p.facts) == 7
    text = fallback_text(p)
    assert text.splitlines()[0].startswith("UGROCAP LIMITED (UGROCAP): 20 in 1 stored day(s), 20 Nov 2026 to 20 Nov 2026; the latest 6")
    assert len(text.splitlines()) <= 10 and validate_text(text, p) == []


def test_short_selling_rows_and_a_stock_with_no_deals():
    p = asm().large_deals(query="KARURVYSYA", kind="short")
    assert fallback_text(p).splitlines()[1] == "Short deal, 19 Nov 2026: KARURVYSYA: 72,850 shares in the short-selling report."
    none = asm().large_deals(query="TCS")
    text = fallback_text(none)
    assert text.splitlines()[0].startswith("No matching bulk, block or short-selling records for TCS LIMITED (TCS) on the stored days (2 stored day(s)")
    assert "No matching deals were found" not in text and validate_text(text, none) == []


def test_a_named_day_and_unknown_inputs():
    p = asm().large_deals(query="GALLANTT", on="2026-11-20")
    assert p.status == "ok" and p.facts[0].value == "4 in 1 stored day(s), 20 Nov 2026 to 20 Nov 2026"
    assert asm().large_deals(query="GALLANTT", on="2026-11-16").gaps[0].code == "no_flow_record"      # no deal data that day
    assert asm().large_deals(query="GALLANTT", on="2026-11-12").gaps[0].code == "no_flow_record"
    assert asm().large_deals(query="GALLANTT", on="banana").gaps[0].code == "bad_date"
    assert asm().large_deals(query="GALLANTT", kind="short").status == "ok"                  # nothing for it in the short list
    clar = asm().large_deals(query="NOSUCHCO")
    assert clar.kind == "clarify"


# ================================================================ market-wide deals
def test_market_wide_counts_and_the_largest_by_value_for_one_kind():
    top = sorted(BULK, key=lambda r: -(r[3] * r[4]))[:5]                 # independent of the assembler's own sort
    p = asm().large_deals(kind="bulk")
    assert (p.kind, p.status) == ("deals_market", "ok")
    lines = fallback_text(p).splitlines()
    assert lines[0] == "Bulk records, 20 Nov 2026: 108."
    assert lines[1] == ("Largest bulk, 20 Nov 2026: V2RETAIL: SELL 6,092,959 shares at ₹161.82 (about ₹98.60 crore, quantity "
                        "times price), client QE SECURITIES LLP.")
    assert [l.split(": ")[1] for l in lines[1:6]] == [f"{r[0]}" for r in top]
    assert len(lines) <= 10 and validate_text("\n".join(lines), p) == []


def test_all_kinds_show_counts_two_each_and_the_short_report_date_and_overlap():
    p = asm().large_deals()
    text = fallback_text(p)
    lines = text.splitlines()
    assert lines[:3] == ["Bulk records, 20 Nov 2026: 108.", "Block records, 20 Nov 2026: 5.", "Short records, 19 Nov 2026: 84."]
    assert sum(1 for l in lines if l.startswith("Largest bulk")) == 2 and sum(1 for l in lines if l.startswith("Largest block")) == 2
    assert "appear in both the bulk and the block list" in text          # the same GALLANTT trade is in both lists
    assert len(lines) <= 10 and validate_text(text, p) == []


def test_short_selling_ranks_by_quantity_and_flags_a_report_that_is_a_day_behind():
    p = asm().large_deals(kind="short")
    lines = fallback_text(p).splitlines()
    assert lines[0] == "Short records, 19 Nov 2026: 84." and lines[1].startswith("Largest short, 19 Nov 2026: KARURVYSYA: 72,850 shares")
    assert [l.split(": ")[1] for l in lines[1:4]] == ["KARURVYSYA", "IREDA", "PPLPHARMA"]
    assert [g.code for g in p.gaps] == []                                 # short rows are not held to the price-day lag


def test_deals_not_yet_stored_for_the_latest_trading_day_and_nothing_stored_for_a_kind():
    s = make_store(real=False)
    s.put_flow("2026-11-19", {"bulk": BULK[:3], "declared": {"bulk": 3}})
    p = asm(s).large_deals(kind="bulk")
    assert [g.code for g in p.gaps] == ["flows_not_yet"] and "Bulk records, 19 Nov 2026: 3." in fallback_text(p)
    q = asm(s).large_deals(kind="block")
    assert q.status == "refused" and q.gaps[0].code == "no_flow_record"
    r = asm(s).large_deals()                                              # bulk stored, block and short not
    assert [g.code for g in r.gaps] == ["flows_not_yet", "deals_partial"]


def test_every_stock_in_the_real_records_gives_a_validated_message():
    a = asm()
    for sym in SYMBOLS:
        p = a.large_deals(query=sym)
        text = fallback_text(p)
        assert validate_text(text, p) == [], (sym, text, validate_text(text, p))
        assert len(text.splitlines()) <= 10, sym
    assert len(SYMBOLS) > 100


def test_the_validator_catches_a_wrong_flows_or_deals_message():
    p = asm().fii_dii()
    good = fallback_text(p)
    assert validate_text(good.replace("23,596.40", "23,596.41"), p) and validate_text(good.replace("net buying", "net selling") + " It rose because of FIIs.", p)
    assert any("missing key figure" in x for x in validate_text("FIIs bought.", p))
    m = asm().large_deals(kind="bulk")
    assert validate_text(fallback_text(m).replace("98.60", "98.61"), m)


# ================================================================ routes and the whole bot
def test_validate_route_for_the_model_tools():
    assert validate_route("fii_dii", {"period": "1M"}, "2026-11-21").args == {"period": "1m"}
    assert validate_route("fii_dii", {"date": "2026-11-20"}, "2026-11-21").args == {"date": "2026-11-20"}
    assert validate_route("fii_dii", {}, "2026-11-21").args == {}
    d = validate_route("large_deals", {"query": "Tata Steel Limited (TATASTEEL)", "kind": "BULK", "date": "2026-11-20"}, "2026-11-21")
    assert d.args == {"query": "TATASTEEL", "kind": "bulk", "date": "2026-11-20"}
    assert validate_route("large_deals", {"kind": "weird"}, "2026-11-21").args == {}
    for tool, bad in (("fii_dii", {"period": "2y"}), ("fii_dii", {"date": "2026-12-01"}), ("fii_dii", {"date": "20-11-2026"}),
                      ("large_deals", {"date": "2026-02-30"}), ("large_deals", {"query": "x" * 81})):
        with pytest.raises(RouteError):
            validate_route(tool, bad, "2026-11-21")


def bot(store=None):
    return Bot(asm(store), DeadChain(), RESOLVER, now=lambda: NOW)


def test_end_to_end_without_any_model():
    prev = Context("TCS", "Tata Consultancy Services Limited")
    ans = bot().ask("what did the FIIs do today", prev)
    assert ans.kind == "flows" and ans.writer == "code" and ans.route.tool == "fii_dii" and ans.context == prev
    assert ans.text.startswith("FII/FPI on 20 Nov 2026: bought ₹23,596.40 crore") and "Sources: NSE FII/DII data" in ans.text
    assert bot().ask("fii flows this week").text.startswith("FII/DII added up over 5 trading days")
    assert "I have FII/DII data for only" in bot(make_store(flow_days=("2026-11-16",))).ask("fii dii last week").text
    stock = bot().ask("bulk deals in GALLANTT")
    assert stock.kind == "deals" and stock.context == Context("GALLANTT", "GALLANTT LIMITED")
    assert "Bulk deal, 20 Nov 2026: GALLANTT: SELL 1,610,292 shares" in stock.text
    market = bot().ask("bulk deals today")
    assert market.kind == "deals_market" and market.text.startswith("Bulk records, 20 Nov 2026: 108.")
    assert bot().ask("did fiis buy tcs today").text.startswith("FII/DII figures are market-wide daily totals")
    assert bot().ask("fii flows over 2 years").text.startswith("I can give 1 week, 1 month")
    assert bot().ask("did fiis buy or sell today").kind == "flows"
    assert bot().ask("should I buy tcs when fiis are selling").text.startswith("I don't give buy, sell or hold advice")
    assert bot().ask("tcs 1 week return").kind == "return"
