# SPDX-License-Identifier: Apache-2.0
"""Feature 2: compare two stocks, ETFs or NSE indices over one period. Index sides use the real NSE files in
tests/fixtures/indices; stock sides use a small synthetic store (prices are made up, the arithmetic is checked by hand)."""
import itertools
from datetime import datetime
from pathlib import Path

import pytest

from briany.answer import build_footer, fallback_text, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler, Gap, Packet
from briany.indexfile import IndexClient, parse_index_csv
from briany.marketdata import PriceRow
from briany.nse import MemoryCache
from briany.router import Context, RouteError, validate_route
from briany.store import MemoryStore
from briany.symbols import Instrument, Resolver
from test_evidence import FakeNews, FakeNse
from test_index_close import DeadChain, Opener

FIX = Path(__file__).parent / "fixtures" / "indices"
FILES = {p.stem[-8:]: p.read_text(encoding="utf-8") for p in FIX.glob("ind_close_all_*.csv")}
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=IST)
RESOLVER = Resolver([Instrument("TCS", "TATA CONSULTANCY SERV LT"), Instrument("INFY", "INFOSYS LIMITED"),
                     Instrument("TATASTEEL", "TATA STEEL LIMITED"), Instrument("TATAPOWER", "TATA POWER CO LTD")])
META = {"updated": "2026-11-20", "start": "2025-10-06", "end": "2027-01-09", "count": 0, "blocking": 0, "symbols": 0}
PRICES = {                          # day: (TCS, INFY) closes, made up
    "2025-11-20": (3000.0, 1500.0), "2025-12-31": (3100.0, 1600.0), "2026-11-13": (2500.0, 1500.0),
    "2026-11-20": (2400.0, 1560.0)}


def make_store(prices=PRICES):
    s = MemoryStore()
    for day, (tcs, infy) in prices.items():
        s.put_day(day, {"TCS": PriceRow(tcs, tcs, 100), "INFY": PriceRow(infy, infy, 100),
                        "TATASTEEL": PriceRow(100.0, 100.0, 10), "TATAPOWER": PriceRow(100.0, 100.0, 10)})
    s.set_actions([], META)
    return s


def asm(files=None, prices=PRICES, index=True):
    client = IndexClient(cache=MemoryCache(), opener=Opener(files or FILES), sleep=lambda s: None) if index else None
    return Assembler(make_store(prices), FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW, index=client)


def facts(p):
    return {f.label: f for f in p.facts}


# ================================================================ two indices (real files)
def test_two_indices_over_ytd():
    p = asm().compare_returns(["Nifty Bank", "nifty it"], "ytd")
    assert (p.kind, p.status, p.symbol, p.name) == ("compare", "ok", "", "Nifty Bank vs Nifty IT")
    a, b, diff = p.facts
    assert (a.value, b.value, diff.value, diff.raw) == ("+8.48%", "-17.32%", "25.80", 25.8)
    text = fallback_text(p)
    assert text.splitlines()[:4] == [
        "From 31 Dec 2025 to 20 Nov 2026: Nifty Bank +8.48%; Nifty IT -17.32%.",
        "Nifty Bank was higher by 25.80 percentage points.",
        "Nifty Bank: 59,581.85 on 31 Dec 2025; 64,635.20 on 20 Nov 2026.",
        "Nifty IT: 37,884.05 on 31 Dec 2025; 31,322.65 on 20 Nov 2026."]
    assert text.splitlines()[4] == ("Both are changes in index level; dividends are not included unless the index itself is a "
                                    "total-return index.")
    assert validate_text(text, p) == []
    footer = build_footer(p)
    assert footer.startswith("Sources: NSE end-of-day index file") and "Index levels are NSE official" in footer


def test_order_of_the_names_decides_who_is_named_higher_not_who_is_listed_first():
    p = asm().compare_returns(["nifty it", "nifty bank"], "ytd")
    assert fallback_text(p).splitlines()[1] == "Nifty Bank was higher by 25.80 percentage points."
    assert p.facts[2].raw == -25.8                                       # signed difference is first minus second


def test_equal_returns_state_a_zero_difference_with_the_number_present():
    p = asm().compare_returns(["Nifty Services Sector", "Nifty CPSE"], "ytd")    # real: both -1.87% over this period
    assert [f.value for f in p.facts] == ["-1.87%", "-1.87%", "0.00"] and p.facts[2].raw == 0.0
    text = fallback_text(p)
    assert text.splitlines()[1] == "The difference is 0.00 percentage points." and "higher by" not in text
    assert validate_text(text, p) == []


# ================================================================ stocks and mixed pairs
def test_two_stocks_over_one_year():
    p = asm().compare_returns(["tcs", "infosys"], "1y")
    assert p.status == "ok" and p.name == "TATA CONSULTANCY SERV LT (TCS) vs INFOSYS LIMITED (INFY)"
    tcs, infy, diff = p.facts
    assert (tcs.raw, infy.raw, diff.raw) == (-20.0, 4.0, -24.0)         # 3000 -> 2400; 1500 -> 1560
    text = fallback_text(p)
    lines = text.splitlines()
    assert lines[0].startswith("From 20 Nov 2025 to 20 Nov 2026: ") and "-20.00%" in lines[0] and "+4.00%" in lines[0]
    assert lines[1].endswith("was higher by 24.00 percentage points.") and "INFY" in lines[1]
    assert "₹3,000.00 on 20 Nov 2025; ₹2,400.00 on 20 Nov 2026." in lines[2]
    assert lines[4] == "Both are price returns; dividends are not included."
    assert validate_text(text, p) == []
    assert "Sources: NSE end-of-day file" in build_footer(p) and "Prices are NSE official" in build_footer(p)


def test_a_stock_against_an_index_names_both_sources_and_both_kinds_of_figure():
    p = asm().compare_returns(["tcs", "nifty it"], "1w")
    assert p.status == "ok"
    tcs, it, diff = p.facts
    assert (tcs.raw, it.raw, diff.raw) == (-4.0, -1.91, -2.09)          # 2500 -> 2400 is -4.00%; Nifty IT -1.91% (real)
    text = fallback_text(p)
    assert "Stock figures are price returns and index figures are changes in level" in text
    assert validate_text(text, p) == []
    footer = build_footer(p)
    assert "NSE end-of-day file" in footer and "NSE end-of-day index file" in footer
    assert "Prices and index levels are NSE official end-of-day values, not live." in footer


# ================================================================ refusals: nothing is ranked on half the evidence
def test_a_side_that_cannot_be_given_blocks_the_comparison_and_the_other_side_is_still_shown():
    p = asm().compare_returns(["Nifty Capital Goods", "nifty"], "1y")       # Capital Goods did not exist on 20 Nov 2025
    assert (p.kind, p.status) == ("compare", "refused")
    assert p.gaps[0].code == "index_not_in_file" and p.gaps[0].text.startswith("Nifty Capital Goods is not in NSE")
    assert [f.label for f in p.facts] == ["Nifty 50: Change in index level"] and p.facts[0].value == "-1.49%"
    text = fallback_text(p)
    assert text.splitlines()[0] == "I can't compare Nifty Capital Goods vs Nifty 50 for this period."
    assert "higher by" not in text and validate_text(text, p) == []


def test_both_sides_refused_and_vix_has_no_return_to_compare():
    p = asm().compare_returns(["india vix", "Nifty50 Dividend Points"], "ytd")
    assert p.status == "refused" and [g.code for g in p.gaps] == ["index_not_a_price"] * 2 and p.facts == ()
    q = asm().compare_returns(["india vix", "nifty"], "ytd")
    assert q.status == "refused" and q.gaps[0].text.startswith("India VIX is not a price-type index") and len(q.facts) == 1
    assert "India VIX: India VIX" not in fallback_text(q)                         # the name is not said twice
    r = asm().compare_returns(["Nifty Capital Goods", "nifty"], "1y")
    assert r.gaps[0].text.startswith("Nifty Capital Goods is not in NSE's index file")
    assert validate_text(fallback_text(q), q) == []


def test_a_missing_index_file_or_no_history_refuses_with_the_single_return_reason():
    no_start = {k: v for k, v in FILES.items() if k != "13112026"}
    p = asm(files=no_start).compare_returns(["tcs", "nifty"], "1w")
    assert p.status == "refused" and p.gaps[0].code == "index_file_unavailable" and "Nifty 50: " in p.gaps[0].text
    assert asm().compare_returns(["tcs", "infosys"], "6m").gaps[0].code == "insufficient_history"


def test_an_ambiguous_name_asks_which_company_instead_of_guessing():
    p = asm().compare_returns(["tata", "infosys"], "1y")
    assert (p.kind, p.status) == ("clarify", "clarify") and "TATA STEEL LIMITED (TATASTEEL)" in p.gaps[0].text


def test_the_same_instrument_twice_is_refused():
    for pair in (["nifty", "nifty 50"], ["tcs", "tata consultancy"]):
        p = asm().compare_returns(pair, "1w")
        assert p.status == "refused" and p.gaps[0].code == "compare_same"
        assert validate_text(fallback_text(p), p) == []
    assert asm().compare_returns(["tcs"], "1w").gaps[0].code == "compare_needs_two"
    assert asm().compare_returns(["tcs", " "], "1w").gaps[0].code == "compare_needs_two"


def test_two_returns_that_do_not_cover_the_same_dates_are_never_compared():
    class Skewed(Assembler):
        def period_return(self, query, label=None, start=None, end=None):
            p = super().period_return(query, label)
            if query == "nifty it":                                     # pretend this side started a day earlier
                f = list(p.facts)
                f[0] = type(f[0])(f[0].label, f[0].value, f[0].raw, f[0].source, "2026-11-12")
                return Packet(p.kind, p.status, p.symbol, p.name, p.as_of, tuple(f), (), (), p.gaps, True)
            return p
    a = Skewed(make_store(), FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW,
               index=IndexClient(cache=MemoryCache(), opener=Opener(FILES)))
    p = a.compare_returns(["nifty", "nifty it"], "1w")
    assert p.status == "refused" and p.gaps[0].code == "compare_dates_differ" and p.facts == ()


# ================================================================ the validator
def test_the_validator_catches_a_wrong_comparison_message():
    p = asm().compare_returns(["Nifty Bank", "nifty it"], "ytd")
    good = fallback_text(p)
    assert validate_text(good.replace("25.80", "25.81"), p)                   # a wrong difference
    assert validate_text(good.replace("+8.48%", "+8.49%"), p)
    assert validate_text(good.replace("64,635.20", "64,635.21"), p)
    assert any("missing key figure" in x for x in validate_text("Nifty Bank beat Nifty IT.", p))
    assert validate_text(good + " It rose because of banks.", p)


def test_many_index_pairs_all_produce_validated_messages():
    names = [r.name for r in parse_index_csv(FILES["20112026"], "2026-11-20")][:36]
    a, ok, refused = asm(), 0, 0
    for x, y in itertools.combinations(names, 2):
        p = a.compare_returns([x, y], "ytd")
        text = fallback_text(p)
        assert validate_text(text, p) == [], (x, y, text)
        ok += p.status == "ok"
        refused += p.status == "refused"
    assert ok + refused == 630 and ok > 400 and refused > 0                  # some of the first 36 are newer than Dec 2025


# ================================================================ routing contracts
def test_validate_route_for_the_model_tool():
    r = validate_route("compare_returns", {"query_a": "TCS", "query_b": "Infosys", "period": "1Y"}, "2026-11-21")
    assert r.tool == "compare_returns" and r.args == {"queries": ["TCS", "Infosys"], "period": "1y"}
    for bad in ({"query_a": "TCS", "query_b": "tcs", "period": "1y"}, {"query_a": "TCS", "period": "1y"},
                {"query_a": "TCS", "query_b": "INFY", "period": "2y"}, {"query_a": "x" * 81, "query_b": "INFY", "period": "1y"},
                {"query_a": "", "query_b": "INFY", "period": "1y"}):
        with pytest.raises(RouteError):
            validate_route("compare_returns", bad, "2026-11-21")


# ================================================================ the whole bot, no model
def bot():
    return Bot(asm(), DeadChain(), RESOLVER, now=lambda: NOW)


def test_end_to_end_comparison_and_its_unsupported_neighbours():
    prev = Context("TCS", "Tata Consultancy Services Limited")
    ans = bot().ask("compare nifty bank and nifty it ytd", prev)
    assert ans.kind == "compare" and ans.writer == "code" and ans.route.tool == "compare_returns"
    assert ans.text.startswith("From 31 Dec 2025 to 20 Nov 2026: Nifty Bank +8.48%; Nifty IT -17.32%.")
    assert ans.context == prev                                             # a comparison does not change the follow-up subject
    assert "Information only, not investment advice." in ans.text
    mixed = bot().ask("tcs vs nifty it over 1 week")
    assert mixed.kind == "compare" and "TCS" in mixed.text and "Prices and index levels" in mixed.text
    assert bot().ask("compare tcs and infosys").text.startswith("Tell me the period to compare over")
    assert bot().ask("compare tcs, infosys and tata steel over 1 year").text.startswith("I can compare two stocks")
    assert bot().ask("compare tcs and infosys over 2 years").text.startswith("I can give 1 week, 1 month")
    # the rules router cannot resolve the ambiguous 'tata', so it sees one company and says what it can compare; a model
    # route with both names reaches compare_returns and gets the 'Did you mean' question (tested above)
    assert bot().ask("compare tata and infosys 1 year").text.startswith("I can compare two stocks")
    assert bot().ask("tcs 1 year return").kind == "return"                  # a single return is unchanged
