# SPDX-License-Identifier: Apache-2.0
"""Feature 1: index period returns, from NSE's end-of-day index files for the start and end days. Real files: 20 Nov 2026 (end),
13 Nov 2026, 31 Dec 2025 and 20 Nov 2025 (start days). 20 Nov 2025 has 143 rows, 20 Nov 2026 has 169: indices came and went."""
from datetime import datetime
from pathlib import Path

import pytest

from briany.answer import build_footer, fallback_text, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler
from briany.indexfile import IndexClient, parse_index_csv
from briany.nse import MemoryCache
from briany.router import Context
from briany.symbols import find_index
from test_evidence import FakeNews, FakeNse
from test_index_close import HTML_PAGE, RESOLVER, DeadChain, Opener, store

FIX = Path(__file__).parent / "fixtures" / "indices"
FILES = {p.stem[-8:]: p.read_text(encoding="utf-8") for p in FIX.glob("ind_close_all_*.csv")}
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=IST)                     # latest stored close: Fri 20 Nov
DAYS = ("2025-11-20", "2025-12-31", "2026-11-13", "2026-11-20")


def asm(days=DAYS, files=None, index=True, now=NOW):
    client = IndexClient(cache=MemoryCache(), opener=Opener(files or FILES), sleep=lambda s: None) if index else None
    return Assembler(store(*days), FakeNse(), FakeNews(), RESOLVER, now=lambda: now, index=client)


@pytest.mark.parametrize("label,start,pct", [("1w", "13 Nov 2026", "+2.61%"), ("ytd", "31 Dec 2025", "-1.26%"),
                                             ("1y", "20 Nov 2025", "-1.49%")])
def test_nifty_50_returns_from_real_files(label, start, pct):
    p = asm().period_return("nifty", label=label)
    assert (p.kind, p.status, p.symbol) == ("index_return", "ok", "NIFTY 50")
    assert p.facts[2].value == pct and p.facts[1].value == "25,800.60" and p.facts[1].date == "2026-11-20"
    text = fallback_text(p)
    assert text.splitlines()[0] == f"Nifty 50 change in level from {start} to 20 Nov 2026: {pct}."
    assert f"{p.facts[0].value} on {start}; 25,800.60 on 20 Nov 2026." in text.splitlines()[1]
    assert "dividends are not included" in text and validate_text(text, p) == []
    footer = build_footer(p)
    assert "NSE end-of-day index file" in footer and "Index levels are NSE official end-of-day values" in footer


def test_the_percent_is_exact_arithmetic_on_the_two_closes():
    p = asm().period_return("nifty bank", label="ytd")
    a, b = p.facts[0].raw, p.facts[1].raw
    assert (a, b) == (59581.85, 64635.20) and p.facts[2].raw == round((b / a - 1) * 100, 2) == 8.48


def test_the_start_is_the_last_stored_day_on_or_before_the_target_and_its_date_is_shown():
    # target for 1w is 13 Nov; if 13 Nov were not stored the start would be an earlier stored day, and the text names it
    a = asm(days=("2025-12-31", "2026-11-13", "2026-11-20"))
    p = a.index_return(find_index("nifty"), start="2026-11-14")           # Saturday: falls back to Friday 13 Nov
    assert p.status == "ok" and p.facts[0].date == "2026-11-13"
    assert "from 13 Nov 2026 to 20 Nov 2026" in fallback_text(p)


@pytest.mark.parametrize("label,code", [("1m", "insufficient_history"), ("6m", "insufficient_history"),
                                        ("banana", "bad_period")])
def test_no_stored_day_near_the_target_is_refused_not_stretched(label, code):
    p = asm().period_return("nifty", label=label)
    assert p.status == "refused" and p.gaps[0].code == code and p.facts == ()
    assert validate_text(fallback_text(p), p) == []


def test_same_day_and_no_price_data():
    p = asm().index_return(find_index("nifty"), start="2026-11-20")
    assert p.status == "refused" and p.gaps[0].code == "no_data"
    empty = Assembler(store(), FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW,
                      index=IndexClient(opener=Opener(FILES))).period_return("nifty", label="1w")
    assert empty.gaps[0].code == "no_price_data"


def test_an_index_that_did_not_exist_at_the_start_is_refused_with_a_clear_reason():
    start_rows = {r.name for r in parse_index_csv(FILES["20112025"], "2025-11-20")}
    assert "Nifty Capital Goods" not in start_rows and "Nifty 1000" not in start_rows        # real: launched later
    for name in ("Nifty Capital Goods", "Nifty 1000"):
        p = asm().period_return(name, label="1y")
        assert p.status == "refused" and p.gaps[0].code == "index_not_in_file" and p.facts == ()
        assert "20 Nov 2025" in p.gaps[0].text and validate_text(fallback_text(p), p) == []
    ok = asm().period_return("Nifty Capital Goods", label="1w")                               # fine over a period it existed
    assert ok.status == "ok"


def test_volatility_and_dividend_points_have_no_percent_return():
    for name in ("india vix", "Nifty50 Dividend Points"):
        p = asm().period_return(name, label="ytd")
        assert p.status == "refused" and p.gaps[0].code == "index_not_a_price" and p.facts == ()
        assert "close on a day" in fallback_text(p) and validate_text(fallback_text(p), p) == []


def test_a_missing_or_broken_file_at_either_end_is_a_gap_never_another_day():
    no_start = {k: v for k, v in FILES.items() if k != "13112026"}
    p = asm(files=no_start).period_return("nifty", label="1w")
    assert p.status == "refused" and p.gaps[0].code == "index_file_unavailable" and "13 Nov 2026" in p.gaps[0].text
    no_end = {k: v for k, v in FILES.items() if k != "20112026"}
    p = asm(files=no_end).period_return("nifty", label="1w")
    assert p.gaps[0].code == "index_file_unavailable" and "20 Nov 2026" in p.gaps[0].text and p.facts == ()
    html = dict(FILES, **{"13112026": HTML_PAGE})
    assert asm(files=html).period_return("nifty", label="1w").gaps[0].code == "index_file_unavailable"
    assert asm(index=False).period_return("nifty", label="1w").gaps[0].code == "index_file_unavailable"


def test_a_row_that_disagrees_with_itself_blocks_the_return():
    lines = FILES["13112026"].splitlines()
    close_col = lines[0].split(",").index("Closing Index Value")
    i = next(n for n, l in enumerate(lines) if l.startswith("Nifty 50,"))
    cells = lines[i].split(",")
    assert float(cells[close_col]) == 25144.3                                                  # the real close
    cells[close_col] = "26000.00"                                                              # a close above its own high
    broken = "\n".join(lines[:i] + [",".join(cells)] + lines[i + 1:]) + "\n"
    p = asm(files=dict(FILES, **{"13112026": broken})).period_return("nifty", label="1w")
    assert p.status == "refused" and p.gaps[0].code == "index_data_inconsistent"


def test_every_index_over_ytd_gives_a_message_that_passes_the_validator():
    names = [r.name for r in parse_index_csv(FILES["20112026"], "2026-11-20")]
    ok = refused = 0
    for n in names:
        p = asm().period_return(n, label="ytd")
        text = fallback_text(p)
        assert validate_text(text, p) == [], (n, text)
        ok += p.status == "ok"
        refused += p.status == "refused"
    # 169 now - 143 on 31 Dec 2025 = 26 indices launched since (refused: not in the start file), plus VIX and Dividend Points
    assert (ok, refused) == (141, 28)


def test_the_validator_catches_a_wrong_index_return_message():
    p = asm().period_return("nifty", label="ytd")
    good = fallback_text(p)
    assert validate_text(good.replace("-1.26%", "-1.27%"), p)
    assert validate_text(good.replace("25,800.60", "25,800.61"), p)
    assert any("missing key figure" in x for x in validate_text("Nifty 50 changed over the year.", p))


def bot():
    return Bot(asm(), DeadChain(), RESOLVER, now=lambda: NOW)


def test_end_to_end_return_and_follow_ups_without_any_model():
    b = bot()
    ans = b.ask("nifty 1 year return")
    assert ans.kind == "index_return" and ans.writer == "code" and ans.route.tool == "period_return"
    assert ans.text.startswith("Nifty 50 change in level from 20 Nov 2025 to 20 Nov 2026: -1.49%.")
    assert ans.context == Context("NIFTY 50", "Nifty 50")
    assert b.ask("and ytd?", ans.context).text.startswith("Nifty 50 change in level from 31 Dec 2025 to 20 Nov 2026: -1.26%.")
    assert b.ask("nifty 2 year return").text.startswith("I can give 1 week, 1 month")
    assert "not a price-type index" in b.ask("india vix ytd").text
    assert b.ask("nifty today").kind == "index"                                              # a close still works
