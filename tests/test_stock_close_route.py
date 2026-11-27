# SPDX-License-Identifier: Apache-2.0
"""A named stock asked for its price or close is answered with its latest move and close, instead of a market-wide refusal."""
import pytest

from briany import bot as B
from briany.llm import ToolCall
from briany.router import STOCK_PRICE_WORDS
from test_bot import FP, bot

REFUSAL = ToolCall("unsupported", {"reason": "market_data"})
SUMMARY = "Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026."


def ask(question, call=REFUSAL, texts=(SUMMARY,)):
    return bot([FP("groq", call, texts=list(texts))]).ask(question)


@pytest.mark.parametrize("question", ["acme close", "ACME closing price", "acme price today", "ACME INDUSTRIES latest", "what is acme trading at",
                                      "acme ltp", "acme quote"])
def test_a_named_stock_asked_for_its_price_gets_its_latest_move_and_close(question):
    ans = ask(question)
    assert (ans.route.tool, ans.route.source) == ("why_moved", "rules:override")
    assert ans.route.args["query"].lower().startswith("acme") and "date" not in ans.route.args
    assert ans.kind == "move"
    assert ans.text.startswith("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026.")
    assert "That is +8.91% from the previous close of ₹101.00." in ans.text
    assert "Prices are NSE official end-of-day closes, not live quotes." in ans.text
    assert B.STOCK_PRICE not in ans.text and B.UNSUPPORTED["market_data"] not in ans.text


def test_a_date_in_the_question_is_kept():
    ans = ask("acme close yesterday")
    assert ans.route.tool == "why_moved" and ans.route.args == {"query": "acme", "date": "2026-11-09"}
    assert ans.text.startswith("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026.")


def test_a_question_the_model_routes_itself_is_left_alone():
    ans = ask("acme close", call=ToolCall("why_moved", {"query": "acme"}))
    assert ans.route.source == "llm:groq" and ans.kind == "move"


@pytest.mark.parametrize("question", ["acme delivery", "acme turnover", "what is the market wide delivery", "anything at all"])
def test_no_price_word_or_no_stock_keeps_the_refusal(question):
    ans = ask(question)
    assert ans.route.tool == "unsupported" and ans.kind == "fixed"
    assert ans.text in (B.STOCK_PRICE, B.UNSUPPORTED["market_data"])


def test_an_unknown_company_keeps_the_market_wide_refusal():
    ans = ask("hdfc today")
    assert ans.route.tool == "unsupported" and ans.text == B.UNSUPPORTED["market_data"]


@pytest.mark.parametrize("reason", ["comparison", "advice", "prediction", "flows_stock"])
def test_only_the_market_data_refusal_is_overruled(reason):
    ans = ask("acme close", call=ToolCall("unsupported", {"reason": reason}))
    assert ans.route.tool == "unsupported" and ans.text == B.UNSUPPORTED[reason]


def test_the_price_words():
    for word in ("close", "Closed", "closing", "price", "prices", "LTP", "quote", "trading", "traded", "latest", "today", "yesterday"):
        assert STOCK_PRICE_WORDS.search(f"acme {word}")
    for text in ("acme delivery", "acme turnover", "acme results", "disclose", "pricey", "latestly"):
        assert not STOCK_PRICE_WORDS.search(text)
