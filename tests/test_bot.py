import pytest

from briany import bot as B
from briany.answer import WRITER_SYSTEM, fallback_text
from briany.bot import Answer, Bot
from briany.llm import Chain, LlmError, ToolCall
from briany.router import Context
from test_evidence import (FULL_NEWS, FULL_NSE, NOW, RESOLVER, FakeNews, FakeNse, act, ann, assembler, build_store)
from briany.nse_parsers import BoardMeeting, EventItem

DIVIDEND = [act("2026-11-09", "dividend", "Dividend - Rs 5 Per Share", False)]
GOOD = ("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from the previous close of ₹101.00.\n"
        "The median listed stock moved +0.50% that day, with 151 stocks up and 50 down.\n"
        "Possible contributing factors (not confirmed causes): an NSE filing about an analyst meet after the previous "
        "close, a dividend ex-date, a results board meeting and news from Reuters before the open.")
BAD_CAUSAL = GOOD + "\nThe rise was because of the analyst meet."
BAD_NUMBER = GOOD.replace("8.91%", "9.5%")


class FP:
    """A provider that can route and write, with scripted behaviour."""

    def __init__(self, name, call=None, texts=(), route_error=None, write_error=None):
        self.name, self.call, self.texts = name, call, list(texts)
        self.route_error, self.write_error, self.write_calls = route_error, write_error, []

    def route(self, system, user, tools):
        if self.route_error:
            raise self.route_error
        return self.call

    def write(self, system, user, max_tokens=500):
        self.write_calls.append((system, user))
        if self.write_error:
            raise self.write_error
        return self.texts.pop(0)


def bot(providers, store=None, nse=None, news=None):
    return Bot(assembler(store or build_store(actions=DIVIDEND), nse or FULL_NSE(), news or FULL_NEWS()),
               Chain(providers), RESOLVER, now=lambda: NOW)


WHY = ToolCall("why_moved", {"query": "acme"})


# ================================================================ the model's text, when it passes
def test_valid_model_text_is_used_and_the_footer_is_added_by_code():
    p = FP("groq", WHY, [GOOD])
    ans = bot([p]).ask("why is acme up today")
    assert ans.writer == "llm:groq" and ans.kind == "move" and ans.problems == ()
    body, footer = ans.text.split("\n\n", 1)
    assert body == GOOD and footer.startswith("Sources: NSE end-of-day file") and "Information only" in footer
    assert ans.route.source == "llm:groq" and ans.context == Context("ACME", "Acme Industries Limited")
    assert p.write_calls[0][0] == WRITER_SYSTEM and p.write_calls[0][1].startswith("Packet:\n")


def test_rejected_text_moves_to_the_next_provider_and_is_logged_but_never_sent():
    a, b = FP("groq", WHY, [BAD_CAUSAL]), FP("gemini", WHY, [GOOD])
    ans = bot([a, b]).ask("why is acme up today")
    assert ans.writer == "llm:gemini" and any("causal wording" in x for x in ans.problems)
    assert "because" not in ans.text.lower()


def test_when_every_text_is_rejected_the_code_built_message_goes_out():
    ans = bot([FP("groq", WHY, [BAD_CAUSAL]), FP("gemini", WHY, [BAD_NUMBER])]).ask("why is acme up today")
    assert ans.writer == "fallback" and "number not in the data: 9.5%" in ans.problems
    body = ans.text.split("\n\n")[0]
    assert body == fallback_text(ans.packet) and body.startswith("Acme Industries Limited (ACME) closed at ₹110.00")
    assert "9.5" not in ans.text and "because" not in ans.text.lower()


def test_provider_failures_during_writing_also_fall_back():
    ans = bot([FP("groq", WHY, write_error=LlmError("rate_limit")), FP("gemini", WHY, write_error=LlmError("server"))]
              ).ask("why is acme up today")
    assert ans.writer == "fallback" and ans.problems == () and ans.text.startswith("Acme Industries Limited")


def test_with_no_working_provider_rules_route_and_code_writes():
    down = [FP("groq", route_error=LlmError("rate_limit"), write_error=LlmError("rate_limit")),
            FP("gemini", route_error=LlmError("server"), write_error=LlmError("server"))]
    ans = bot(down).ask("why is acme up today")
    assert ans.routed.fell_back and ans.route.source == "rules" and ans.route.tool == "why_moved"
    assert ans.writer == "fallback" and "closed at ₹110.00" in ans.text


def test_missing_caveat_in_model_text_is_rejected():
    text = "Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from the previous close of ₹101.00."
    ans = bot([FP("groq", WHY, [text])], store=build_store(), nse=FakeNse(), news=FakeNews()).ask("why is acme up")
    assert ans.writer == "fallback" and any("no clear evidence found" in x for x in ans.problems)
    assert "No clear evidence found." in ans.text


# ================================================================ answers the model never writes
def test_fixed_answers_for_unsupported_questions_and_smalltalk():
    for reason, text in B.UNSUPPORTED.items():
        p = FP("groq", ToolCall("unsupported", {"reason": reason}))
        ans = bot([p]).ask("anything at all")
        assert ans.text == text and ans.kind == "fixed" and ans.writer == "fixed" and ans.packet is None
        assert p.write_calls == [] and text.strip()
    hi = bot([FP("groq", ToolCall("smalltalk", {}))]).ask("hello")
    assert hi.text == B.HELP and "Information only, not investment advice" in B.HELP


def test_returns_and_events_are_built_by_code_without_the_model():
    store = build_store({"2026-10-09": (90.0, 89.0), "2026-11-09": (110.0, 109.0)}, fillers=0,
                        actions=[act("2026-10-20", "dividend", "Dividend - Rs 5 Per Share", False)])
    p = FP("groq", ToolCall("period_return", {"query": "acme", "period": "1m"}))
    ans = bot([p], store=store).ask("acme 1 month return")
    assert ans.kind == "return" and ans.writer == "code" and p.write_calls == []
    assert "price return from 9 Oct 2026 to 9 Nov 2026: +22.22%" in ans.text and "excludes dividends" in ans.text
    assert ans.text.rsplit("\n\n", 1)[1].startswith("Sources:")
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet")],
                  meetings=[BoardMeeting("ACME", "2026-11-20", "Financial Results", "d", None, True)],
                  next_event=EventItem("ACME", "2026-11-20", "Financial Results", "d"))
    p2 = FP("groq", ToolCall("events", {"query": "acme"}))
    ev = bot([p2], nse=nse).ask("any announcements from acme")
    assert ev.kind == "events" and ev.writer == "code" and p2.write_calls == []
    assert "Next results meeting: 20 Nov 2026." in ev.text


def test_clarifications_and_refusals_never_reach_the_model():
    p = FP("groq", ToolCall("why_moved", {"query": "tata"}))
    ans = bot([p]).ask("why is tata up")
    assert ans.kind == "clarify" and ans.writer == "code" and ans.text.startswith("Did you mean")
    assert "Sources:" not in ans.text and ans.context == Context() and p.write_calls == []
    q = FP("groq", WHY)
    refused = bot([q], store=build_store(meta=None)).ask("why is acme up")
    assert refused.text.startswith("I can't give a reliable figure") and "could not be verified" in refused.text
    assert refused.writer == "code" and q.write_calls == []


def test_the_writer_never_sees_urls_and_gets_the_required_phrases():
    p = FP("groq", WHY, ["unusable text"])
    bot([p], store=build_store(), nse=FakeNse(), news=FakeNews()).ask("why is acme up")
    user = p.write_calls[0][1]
    assert "http" not in user and '"must_include": ["no clear evidence found"]' in user


# ================================================================ robustness
def test_follow_up_questions_reuse_the_previous_company():
    first = bot([FP("groq", WHY, [GOOD])]).ask("why is acme up today")
    store = build_store({"2026-10-09": (90.0, 89.0), "2026-11-09": (110.0, 109.0)}, fillers=0)
    follow = bot([FP("groq", ToolCall("period_return", {"query": "ACME", "period": "1m"}))], store=store
                 ).ask("and last month?", first.context)
    assert follow.kind == "return" and follow.context.last_symbol == "ACME"


def test_an_unexpected_error_still_produces_a_message_and_logs_only_the_type():
    class Boom:
        def why_moved(self, *a, **k):
            raise RuntimeError("secret internal detail")
    ans = Bot(Boom(), Chain([FP("groq", WHY)]), RESOLVER, now=lambda: NOW).ask("why is acme up")
    assert ans.text == B.ERROR_TEXT and ans.writer == "error" and ans.problems == ("RuntimeError",)
    assert "secret" not in ans.text and ans.context == Context()
    assert isinstance(ans, Answer)
