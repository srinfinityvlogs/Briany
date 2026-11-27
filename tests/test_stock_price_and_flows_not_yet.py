# SPDX-License-Identifier: Apache-2.0
"""Two small answer fixes: a named stock asked for its price (not a market-wide figure), and an FII/DII question for a day that
NSE has not published yet (show the latest stored day, say which it is)."""
from datetime import datetime

from briany import bot as B
from briany.answer import fallback_text, validate_text
from briany.bot import Bot
from briany.evidence import IST
from briany.llm import Chain, ToolCall
from briany.store import MemoryStore
from test_bot import FP, bot
from test_flow_answers import PRICE_DAYS, RESOLVER, asm, fii_rows, make_store


# ================================================================ a named stock is not a market-wide figure
def unsupported(reason, question):
    return bot([FP("groq", ToolCall("unsupported", {"reason": reason}))]).ask(question)


def test_a_named_stock_asked_for_a_price_hears_what_can_be_asked_about_a_stock():
    for question in ("acme delivery", "acme turnover figures", "ACME INDUSTRIES volume"):      # no price word: the stock-close route does not apply
        ans = unsupported("market_data", question)
        assert (ans.text, ans.kind, ans.writer, ans.packet) == (B.STOCK_PRICE, "fixed", "fixed", None)
    assert "single stock" in B.STOCK_PRICE and "Nifty 50 close today" in B.STOCK_PRICE and "market-wide" not in B.STOCK_PRICE


def test_a_market_wide_question_keeps_the_market_wide_message():
    for question in ("what is the market wide delivery", "anything at all"):
        assert unsupported("market_data", question).text == B.UNSUPPORTED["market_data"]


def test_only_the_market_data_reason_changes_for_a_named_stock():
    for reason in ("comparison", "compare_period", "advice", "prediction", "flows_stock", "index_not_covered", "period"):
        assert unsupported(reason, "acme close").text == B.UNSUPPORTED[reason]


# ================================================================ FII/DII for a day NSE has not published yet
DAYS_TO_20 = make_store()                       # prices to Fri 20 Nov; FII/DII stored 16 to 20 Nov


def test_a_day_after_the_latest_priced_day_shows_the_latest_stored_day_and_says_so():
    p = asm(make_store()).fii_dii(on="2026-11-23")
    assert p.status == "ok" and [g.code for g in p.gaps] == ["flows_not_yet"]
    text = fallback_text(p)
    assert text.startswith("FII/FPI on 20 Nov 2026: bought ₹23,596.40 crore")
    assert "FII/DII data for 23 Nov 2026 is not stored yet; this is the latest day stored." in text
    assert validate_text(text, p) == []


def test_a_past_day_that_is_missing_is_still_refused_because_it_cannot_be_added():
    s = make_store(real=False)                                   # prices to 20 Nov, FII/DII only 16 to 19 Nov
    for day in ("2026-11-20", "2026-11-14", "2026-11-12"):       # the latest priced day, a Saturday, before storage began
        q = asm(s).fii_dii(on=day)
        assert q.status == "refused" and q.gaps[0].code == "no_flow_record"
    assert "earlier days cannot be added" in asm(s).fii_dii(on="2026-11-20").gaps[0].text


def test_a_stored_day_is_never_replaced_by_another():
    p = asm(make_store()).fii_dii(on="2026-11-18")
    assert p.gaps == () and fallback_text(p).startswith("FII/FPI on 18 Nov 2026")


def test_a_later_day_does_not_fall_back_when_nothing_holds_fii_data():
    s = make_store(flow_days=(), real=False)
    s.put_flow("2026-11-19", {"short": [["AAA", 5]]})
    assert asm(s).fii_dii(on="2026-11-23").gaps[0].code == "no_flow_record"


def test_a_question_the_model_routes_with_todays_date_before_nse_publishes_gets_the_latest_stored_day():
    now = datetime(2026, 11, 23, 18, 0, tzinfo=IST)               # Monday evening, before the day's file is stored
    model = FP("groq", ToolCall("fii_dii", {"date": "2026-11-23"}))
    answer = Bot(asm(make_store(), now=now), Chain([model]), RESOLVER, now=lambda: now).ask("how are the big foreign investors doing")
    assert answer.route.source == "llm:groq" and answer.route.args == {"date": "2026-11-23"}
    assert answer.kind == "flows" and answer.text.startswith("FII/FPI on 20 Nov 2026")
    assert "FII/DII data for 23 Nov 2026 is not stored yet; this is the latest day stored." in answer.text


# ================================================================ "records start on" names the first day with FII/DII
def only_deals_first():
    s = make_store(flow_days=(), real=False, prices=PRICE_DAYS)
    s.put_flow("2026-11-16", {"short": [["AAA", 5]]})             # the first record holds deals only
    for d in ("2026-11-17", "2026-11-18"):
        s.put_flow(d, {"fii": fii_rows()})
    return s


def test_records_start_on_the_first_day_that_holds_fii_dii_data():
    q = asm(only_deals_first()).fii_dii(on="2026-11-14")
    assert q.gaps[0].code == "no_flow_record" and "records start on 17 Nov 2026" in q.gaps[0].text


def test_a_period_with_no_fii_dii_in_it_says_when_records_start():
    s = make_store(flow_days=(), real=False)
    s.put_flow("2026-11-12", {"short": [["AAA", 5]]})             # the first record holds deals only
    s.put_flow("2026-11-13", {"fii": fii_rows()})                 # FII/DII starts here, before the week asked about
    q = asm(s).fii_dii(label="1w")
    assert q.kind == "flows_period" and q.status == "refused"
    assert q.gaps[0].text == "No FII/DII data is stored for 16 Nov 2026 to 20 Nov 2026; records start on 13 Nov 2026."
