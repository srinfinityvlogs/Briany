import pytest

from briany.llm import Chain, LlmError, ToolCall
from briany.router import (Context, RouteError, find_companies, parse_date, parse_period, route_question,
                           rule_route, validate_route)
from briany.symbols import Instrument, Resolver
from datetime import date

TODAY = "2026-11-10"
RESOLVER = Resolver([Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD"), Instrument("TCS", "TATA CONSULTANCY SERV LT"),
                     Instrument("INFY", "INFOSYS LIMITED"), Instrument("TATASTEEL", "TATA STEEL LIMITED"),
                     Instrument("TATAPOWER", "TATA POWER CO LTD"), Instrument("HDFCBANK", "HDFC BANK LTD"),
                     Instrument("ANGELONE", "ANGEL ONE LIMITED")])


# ---------------------------------------------------------------- validation of model output
def test_valid_routes_are_cleaned():
    assert validate_route("why_moved", {"query": "  tata steel ", "date": "2026-11-05"}, TODAY).args == {
        "query": "tata steel", "date": "2026-11-05"}
    assert validate_route("why_moved", {"query": "tcs", "date": ""}, TODAY).args == {"query": "tcs"}
    assert validate_route("period_return", {"query": "tcs", "period": "6M"}, TODAY).args == {"query": "tcs", "period": "6m"}
    assert validate_route("events", {"query": "infy", "junk": 1}, TODAY).args == {"query": "infy"}
    assert validate_route("unsupported", {"reason": "weird"}, TODAY).args == {"reason": "other"}
    assert validate_route("unsupported", {"reason": "advice"}, TODAY).args == {"reason": "advice"}
    assert validate_route("smalltalk", {"x": 1}, TODAY).args == {}


@pytest.mark.parametrize("name,args", [
    ("why_moved", {}), ("why_moved", {"query": ""}), ("why_moved", {"query": "x" * 81}),
    ("why_moved", {"query": "tcs", "date": "05-11-2026"}), ("why_moved", {"query": "tcs", "date": "2026-02-30"}),
    ("why_moved", {"query": "tcs", "date": "2026-11-11"}),                      # tomorrow
    ("period_return", {"query": "tcs"}), ("period_return", {"query": "tcs", "period": "2y"}),
    ("events", {"query": None}), ("delete_everything", {}), ("", {})])
def test_invalid_routes_are_rejected(name, args):
    with pytest.raises(RouteError):
        validate_route(name, args, TODAY)


# ---------------------------------------------------------------- helpers
def test_parse_period():
    cases = {"tcs 6 month return": ("6m", True), "returns this year": ("ytd", True), "last month": ("1m", True),
             "past week": ("1w", True), "1 year performance": ("1y", True), "12 months": ("1y", True),
             "3-month gain": ("3m", True), "2 year return": (None, True), "5 months": (None, True),
             "why is it up": (None, False)}
    for text, expected in cases.items():
        assert parse_period(text) == expected, text


def test_parse_date_with_year_inference():
    today = date(2026, 11, 10)
    assert parse_date("on 5 Nov", today) == "2026-11-05"
    assert parse_date("on Nov 5th", today) == "2026-11-05"
    assert parse_date("25 december", today) == "2025-12-25"            # would be in the future: previous year
    assert parse_date("3 march 2024", today) == "2024-03-03"
    assert parse_date("on 2026-11-05", today) == "2026-11-05" and parse_date("05/11/2026", today) == "2026-11-05"
    assert parse_date("yesterday", today) == "2026-11-09"
    assert parse_date("31 feb", today) is None and parse_date("the market rose in march", today) is None


def test_find_companies_prefers_longer_mentions_and_skips_indices_and_noise():
    assert find_companies("why is tata steel up", RESOLVER) == [("tata steel", "TATASTEEL")]
    assert find_companies("tcs vs infosys", RESOLVER) == [("tcs", "TCS"), ("infosys", "INFY")]
    assert find_companies("nifty 50 return", RESOLVER) == []
    assert find_companies("why is it down today", RESOLVER) == []
    assert find_companies("how is relience doing", RESOLVER)[0][1] == "RELIANCE"        # typo, 8 letters


# ---------------------------------------------------------------- rule router
RULE_CASES = [
    ("why is tata steel up 8% today", "why_moved", {"query": "tata steel"}),
    ("Why did Reliance fall on 5 Nov?", "why_moved", {"query": "Reliance", "date": "2026-11-05"}),
    ("what happened to TCS yesterday", "why_moved", {"query": "TCS", "date": "2026-11-09"}),
    ("reliance", "why_moved", {"query": "reliance"}),
    ("TCS 6 month return", "period_return", {"query": "TCS", "period": "6m"}),
    ("infosys returns this year", "period_return", {"query": "infosys", "period": "ytd"}),
    ("how much has hdfc bank gained in the last 3 months", "period_return", {"query": "hdfc bank", "period": "3m"}),
    ("reliance 2 year return", "unsupported", {"reason": "period"}),
    ("when are reliance results due", "events", {"query": "reliance"}),
    ("any announcements from angel one", "events", {"query": "angel one"}),
    ("compare tcs and infosys", "unsupported", {"reason": "comparison"}),
    ("tcs vs infy", "unsupported", {"reason": "comparison"}),
    ("should I buy reliance", "unsupported", {"reason": "advice"}),
    ("will reliance go up tomorrow", "unsupported", {"reason": "prediction"}),
    ("what did FII do today", "unsupported", {"reason": "market_data"}),
    ("nifty 50 return", "unsupported", {"reason": "market_data"}),
    ("some unknown company news", "unsupported", {"reason": "company_not_found"}),
    ("hello", "smalltalk", {}), ("/start", "smalltalk", {}), ("thanks", "smalltalk", {}),
]


@pytest.mark.parametrize("question,tool,args", RULE_CASES)
def test_rule_router(question, tool, args):
    r = rule_route(question, RESOLVER, TODAY)
    assert (r.tool, r.args, r.source) == (tool, args, "rules")


def test_follow_ups_use_the_previous_company_only_when_there_is_one():
    ctx = Context(last_symbol="RELIANCE", last_name="Reliance Industries Limited")
    assert rule_route("and last month?", RESOLVER, TODAY, ctx).args == {"query": "RELIANCE", "period": "1m"}
    assert rule_route("why did it fall?", RESOLVER, TODAY, ctx).args == {"query": "RELIANCE"}
    assert rule_route("why did it fall?", RESOLVER, TODAY).args == {"reason": "company_not_found"}
    assert rule_route("what about TCS?", RESOLVER, TODAY, ctx).args == {"query": "TCS"}        # a new company wins


# ---------------------------------------------------------------- full routing with the chain
class P:
    def __init__(self, name, call=None, error=None):
        self.name, self.call, self.error, self.system = name, call, error, None

    def route(self, system, user, tools):
        self.system = system
        if self.error:
            raise self.error
        return self.call


def test_llm_route_is_used_validated_and_labelled():
    p = P("groq", ToolCall("period_return", {"query": "tcs", "period": "1M"}))
    out = route_question("tcs one month return", Chain([p]), RESOLVER, TODAY)
    assert out.route.tool == "period_return" and out.route.args == {"query": "tcs", "period": "1m"}
    assert out.route.source == "llm:groq" and not out.fell_back and out.attempts == (("groq", "ok"),)
    assert f"Today is {TODAY}" in p.system and "No previous company" in p.system


def test_context_reaches_the_prompt():
    p = P("groq", ToolCall("smalltalk", {}))
    route_question("and last month?", Chain([p]), RESOLVER, TODAY, Context("RELIANCE", "Reliance Industries Limited"))
    assert "Previous company in this conversation: Reliance Industries Limited (RELIANCE)" in p.system


def test_bad_model_output_moves_to_the_next_provider():
    bad = P("groq", ToolCall("why_moved", {"query": "tcs", "date": "2099-01-01"}))             # future date
    good = P("gemini", ToolCall("why_moved", {"query": "tcs"}))
    out = route_question("why is tcs up", Chain([bad, good]), RESOLVER, TODAY)
    assert out.route.source == "llm:gemini" and out.attempts == (("groq", "invalid_route"), ("gemini", "ok"))


def test_when_every_provider_fails_the_rules_answer_the_question():
    chain = Chain([P("groq", error=LlmError("rate_limit")), P("gemini", error=LlmError("server"))])
    out = route_question("why did reliance fall on 5 Nov", chain, RESOLVER, TODAY)
    assert out.fell_back and out.route.source == "rules"
    assert out.route.args == {"query": "reliance", "date": "2026-11-05"}
    assert out.attempts == (("groq", "rate_limit"), ("gemini", "server"))
    assert route_question("hello", Chain([]), RESOLVER, TODAY).route.tool == "smalltalk"
    assert route_question("   ", Chain([P("groq", error=LlmError("server"))]), RESOLVER, TODAY).route.tool == "smalltalk"


def test_plural_flow_words_and_capability_questions():
    assert rule_route("what are FIIs doing today", RESOLVER, TODAY).args == {"reason": "market_data"}
    assert rule_route("how are DIIs positioned", RESOLVER, TODAY).args == {"reason": "market_data"}
    for q in ("what can you do", "Who are you?", "how does this work", "can you help me"):
        assert rule_route(q, RESOLVER, TODAY).tool == "smalltalk", q
    assert rule_route("what can you tell me about reliance results", RESOLVER, TODAY).tool == "events"
