# SPDX-License-Identifier: Apache-2.0
"""A symbol in none of the previous 20 stored trading days is possibly newly listed: NSE's previous-close field is then an issue
or reference price, so no 'change from the previous close' is given. One missing day alone proves nothing (illiquid stocks skip
days), and with fewer than 20 earlier days stored nothing is claimed."""
from datetime import date, datetime, timedelta

import pytest

from briany.answer import build_footer, fallback_text, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler
from briany.marketdata import LISTING_LOOKBACK, PriceRow, daily_move
from briany.nse_parsers import CorpAction, parse_detail
from briany.router import Context
from briany.store import MemoryStore
from briany.symbols import Instrument, Resolver
from test_evidence import FakeNews, FakeNse
from test_index_close import DeadChain

META = {"updated": "2026-11-20", "start": "2025-10-06", "end": "2027-01-09", "count": 1, "blocking": 1, "symbols": 1}
RESOLVER = Resolver([Instrument("TCS", "TATA CONSULTANCY SERV LT"), Instrument("NEWCO", "NEWCO LIMITED"),
                     Instrument("ILLIQ", "ILLIQ LIMITED")])


def weekdays(n, end="2026-11-20"):
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def build(n_days=25, new=None, seen=(), actions=()):
    """n_days stored weekdays ending 20 Nov 2026. TCS trades every day. `new` = (close, prev_close) for NEWCO on the last day;
    `seen` = indexes (0 = first stored day) of other days on which NEWCO also traded."""
    days = weekdays(n_days)
    s = MemoryStore()
    for i, d in enumerate(days):
        rows = {"TCS": PriceRow(100.0, 100.0, 10)}
        if new and i == len(days) - 1:
            rows["NEWCO"] = PriceRow(*new, 10)
        elif i in seen:
            rows["NEWCO"] = PriceRow(100.0, 100.0, 10)
        s.put_day(d, rows)
    s.set_actions(list(actions), META)
    return s, days


def move(store, day="2026-11-20", symbol="NEWCO"):
    return daily_move(store, symbol, day)


# ================================================================ the rule itself
def test_a_new_listing_has_no_day_to_day_move_even_with_a_big_pop():
    s, _ = build(new=(140.0, 100.0))                  # lists at 40% over its issue price of 100
    m = move(s)
    assert m.status == "possible_listing" and m.pct is None and (m.close, m.prev_close) == (140.0, 100.0)
    assert "issue or reference price" in m.note
    s2, _ = build(new=(105.0, 100.0))                 # an ordinary-looking +5% is also not a day-to-day move
    assert move(s2).status == "possible_listing" and move(s2).pct is None


def test_an_illiquid_stock_that_skipped_a_day_is_still_a_normal_move():
    s, days = build(new=(105.0, 100.0), seen=(len(days := weekdays(25)) - 4,))     # traded 3 days before, not yesterday
    m = move(s)
    assert m.status == "ok" and round(m.pct, 2) == 5.0


def test_the_lookback_boundary_is_exactly_twenty_trading_days():
    last = len(weekdays(25)) - 1
    inside, _ = build(new=(105.0, 100.0), seen=(last - LISTING_LOOKBACK,))        # seen on the 20th day before: not a listing
    assert move(inside).status == "ok"
    outside, _ = build(new=(105.0, 100.0), seen=(last - LISTING_LOOKBACK - 1,))   # 21 days before: outside the window
    assert move(outside).status == "possible_listing"


def test_with_too_little_history_nothing_is_claimed():
    s, _ = build(n_days=LISTING_LOOKBACK, new=(105.0, 100.0))     # only 19 earlier days stored
    assert move(s).status == "ok"
    s, _ = build(n_days=LISTING_LOOKBACK + 1, new=(105.0, 100.0))  # exactly 20 earlier days
    assert move(s).status == "possible_listing"


def test_a_symbol_that_trades_every_day_is_never_flagged_and_the_check_is_cheap_when_it_does():
    s, _ = build(new=None)
    calls = []
    real = s.get_row
    s.get_row = lambda d, sym: (calls.append(d), real(d, sym))[1]
    m = move(s, symbol="TCS")
    assert m.status == "ok" and m.pct == 0.0
    assert len(calls) <= 3 + LISTING_LOOKBACK                       # at most one early exit plus the row lookups, not the whole history


# ================================================================ the packet, the text and the bot
def asm(store, now=datetime(2026, 11, 20, 20, 0, tzinfo=IST)):
    return Assembler(store, FakeNse(), FakeNews(), RESOLVER, now=lambda: now)


def test_the_packet_gives_the_reference_price_and_no_change_figure():
    s, _ = build(new=(140.0, 100.0))
    p = asm(s).why_moved("newco", on="2026-11-20")
    assert (p.kind, p.status) == ("move", "ok")
    labels = [f.label for f in p.facts]
    assert labels == ["Official close, 20 Nov 2026", "Reference price (NSE's previous-close field)"] and p.facts[1].date is None
    assert not any(f.label.startswith("Change") for f in p.facts)
    assert [g.code for g in p.gaps if g.code == "possible_listing"] == ["possible_listing"]
    assert p.factors == () and p.evidence_complete


def test_the_text_says_it_may_be_a_first_trading_day_and_never_a_percentage_move():
    s, _ = build(new=(140.0, 100.0))
    p = asm(s).why_moved("newco", on="2026-11-20")
    text = fallback_text(p)
    assert "first trading day" in text and "no change from the previous close is given" in text
    assert "NSE's reference price (its previous-close field) was ₹100.00, so no percentage change is shown." in text
    assert "unadjusted" not in text and "%" not in text
    assert validate_text(text, p) == []
    assert validate_text("NEWCO LIMITED rose 40% on its listing.", p)           # a model-written percentage is rejected
    no_caveat = "\n".join(l for l in text.splitlines() if "first trading day" not in l)
    assert validate_text(no_caveat, p) == ["required caveat missing (possible_listing): include 'first trading day'"]


def test_a_normal_stock_answer_is_unchanged():
    s, _ = build(new=None)
    p = asm(s).why_moved("tcs", on="2026-11-20")
    assert [f.label for f in p.facts][:3] == ["Official close, 20 Nov 2026", "Previous close", "Change from previous close"]
    assert all(g.code != "possible_listing" for g in p.gaps)


def test_end_to_end_without_a_model():
    s, _ = build(new=(140.0, 100.0))
    ans = Bot(asm(s), DeadChain(), RESOLVER, now=lambda: datetime(2026, 11, 20, 20, 0, tzinfo=IST)).ask("why did newco jump today")
    assert ans.kind == "move" and "first trading day" in ans.text and "%" not in ans.text and "unadjusted" not in ans.text


def test_a_real_split_on_the_day_is_reported_as_a_corporate_action_not_a_listing():
    a = CorpAction("NEWCO", "NEWCO", "2026-11-20", None, "Bonus 1:1", "bonus", True, detail=parse_detail("bonus", "Bonus 1:1"))
    s, _ = build(new=(70.0, 100.0), actions=[a])
    p = asm(s).why_moved("newco", on="2026-11-20")
    codes = [g.code for g in p.gaps]
    assert "corporate_action_on_date" in codes and "possible_listing" not in codes
    assert "(unadjusted)" in fallback_text(p)
