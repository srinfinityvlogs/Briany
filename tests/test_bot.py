# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from briany import bot as B
from briany.answer import SUMMARY_SYSTEM, fallback_text
from briany.bot import Answer, Bot
from briany.llm import Chain, LlmError, ToolCall
from briany.nse_parsers import BoardMeeting, EventItem
from briany.router import Context
from test_evidence import (FULL_NEWS, FULL_NSE, NOW, RESOLVER, FakeNews, FakeNse, act, ann, assembler, build_store)

DIVIDEND = [act("2026-11-09", "dividend", "Dividend - Rs 5 Per Share", False)]
SUMMARY = ("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from the previous close of ₹101.00.\n"
           "The median listed stock moved +0.50% that day, with 151 stocks up and 50 down.")
BAD_CAUSAL = SUMMARY + " The rise was because of the analyst meet."
BAD_NUMBER = SUMMARY.replace("8.91%", "9.5%")
NO_DATE = SUMMARY.replace(" on 9 Nov 2026", "")
ABOUT_NEWS = SUMMARY + " News from Reuters is also relevant."


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
        self.write_calls.append((system, user, max_tokens))
        if self.write_error:
            raise self.write_error
        return self.texts.pop(0)


def bot(providers, store=None, nse=None, news=None):
    return Bot(assembler(store or build_store(actions=DIVIDEND), nse or FULL_NSE(), news or FULL_NEWS()),
               Chain(providers), RESOLVER, now=lambda: NOW)


WHY = ToolCall("why_moved", {"query": "acme"})


# ================================================================ the model writes the summary; code adds the rest
def test_valid_summary_is_used_and_code_adds_factors_caveats_and_footer():
    p = FP("groq", WHY, [SUMMARY])
    ans = bot([p]).ask("why is acme up today")
    assert ans.writer == "llm:groq" and ans.kind == "move" and ans.problems == ()
    body, footer = ans.text.split("\n\n", 1)
    assert body.startswith(SUMMARY) and "Possible contributing factors (not confirmed causes):" in body
    assert "News (Reuters)" in body and "Outcome of Board Meeting" in body and "Corporate action, ex-date" in body
    assert footer.startswith("Sources: NSE end-of-day file") and "Information only" in footer
    assert ans.write_attempts == (("groq", "ok"),) and ans.route.source == "llm:groq"
    assert ans.context == Context("ACME", "Acme Industries Limited")


def test_the_writer_gets_a_small_prompt_with_facts_only():
    p = FP("groq", WHY, [SUMMARY])
    bot([p]).ask("why is acme up today")
    system, user, max_tokens = p.write_calls[0]
    assert system == SUMMARY_SYSTEM and max_tokens >= 900                                # room for hidden reasoning tokens
    payload = json.loads(user.split("Packet:\n", 1)[1].rsplit("\nWrite", 1)[0])
    assert set(payload) == {"company", "symbol", "facts", "market_context"} and "http" not in user


def test_rejected_summary_moves_to_the_next_provider_and_is_logged_but_never_sent():
    a, b = FP("groq", WHY, [BAD_CAUSAL]), FP("gemini", WHY, [SUMMARY])
    ans = bot([a, b]).ask("why is acme up today")
    assert ans.writer == "llm:gemini" and any("causal wording" in x for x in ans.problems)
    assert ans.write_attempts == (("groq", "invalid_text"), ("gemini", "ok")) and "because" not in ans.text.lower()


@pytest.mark.parametrize("bad,reason", [
    (BAD_NUMBER, "number not in the data: 9.5%"),
    (NO_DATE, "missing key figure: the date of the close"),
    (ABOUT_NEWS, "must not discuss factors, filings or news")])
def test_every_kind_of_bad_summary_ends_in_the_code_built_message(bad, reason):
    ans = bot([FP("groq", WHY, [bad]), FP("gemini", WHY, [bad])]).ask("why is acme up today")
    assert ans.writer == "fallback" and any(reason in x for x in ans.problems)
    assert ans.text.split("\n\n")[0] == fallback_text(ans.packet)
    assert ans.write_attempts == (("groq", "invalid_text"), ("gemini", "invalid_text"))


def test_provider_failures_during_writing_also_fall_back_and_are_recorded():
    ans = bot([FP("groq", WHY, write_error=LlmError("rate_limit")), FP("gemini", WHY, write_error=LlmError("server"))]
              ).ask("why is acme up today")
    assert ans.writer == "fallback" and ans.problems == () and ans.text.startswith("Acme Industries Limited")
    assert ans.write_attempts == (("groq", "rate_limit"), ("gemini", "server"))


def test_with_no_working_provider_rules_route_and_code_writes():
    down = [FP("groq", route_error=LlmError("rate_limit"), write_error=LlmError("rate_limit")),
            FP("gemini", route_error=LlmError("server"), write_error=LlmError("server"))]
    ans = bot(down).ask("why is acme up today")
    assert ans.routed.fell_back and ans.route.source == "rules" and ans.route.tool == "why_moved"
    assert ans.writer == "fallback" and "closed at ₹110.00" in ans.text


def test_a_summary_cannot_drop_the_caveats_because_code_writes_them():
    ans = bot([FP("groq", WHY, [SUMMARY])], store=build_store(), nse=FakeNse(), news=FakeNews()).ask("why is acme up")
    assert ans.writer == "llm:groq" and "No clear evidence found." in ans.text
    failed = bot([FP("groq", WHY, [SUMMARY])], nse=FakeNse(fail=("announcements",)), news=FakeNews(status="error")
                 ).ask("why is acme up")
    assert failed.writer == "llm:groq" and failed.text.count("could not be checked") >= 2


def test_the_assembled_message_gets_a_last_check(monkeypatch):
    monkeypatch.setattr(B, "compose_move", lambda p, s, notes=(), shown_out=None: "Acme closed at ₹110.00 and ₹777.00.")
    ans = bot([FP("groq", WHY, [SUMMARY])]).ask("why is acme up today")
    assert ans.writer == "fallback" and any(x.startswith("assembled message:") for x in ans.problems)
    assert "777" not in ans.text


def test_a_premise_the_data_contradicts_is_called_out_in_code():
    ans = bot([FP("groq", WHY, [SUMMARY])]).ask("why is acme down today")
    lines = ans.text.splitlines()
    assert lines[2] == "Note: the change from the previous close was +8.91%, which is not a decrease."
    code = bot([FP("groq", WHY, write_error=LlmError("server"))]).ask("why is acme down today")
    assert "which is not a decrease." in code.text
    assert "Note:" not in bot([FP("groq", WHY, [SUMMARY])]).ask("why is acme up today").text


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
    assert "Prices are NSE" not in ev.text                                    # an events answer carries no price note


def test_clarifications_and_refusals_never_reach_the_model():
    p = FP("groq", ToolCall("why_moved", {"query": "tata"}))
    ans = bot([p]).ask("why is tata up")
    assert ans.kind == "clarify" and ans.writer == "code" and ans.text.startswith("Did you mean")
    assert "Sources:" not in ans.text and ans.context == Context() and p.write_calls == []
    q = FP("groq", WHY)
    refused = bot([q], store=build_store(meta=None)).ask("why is acme up")
    assert refused.text.startswith("I can't give a reliable figure") and "could not be verified" in refused.text
    assert refused.writer == "code" and q.write_calls == []


def test_a_verified_split_day_is_stated_once_and_not_as_a_hedged_factor():
    store = build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (50.5, 101.0)},
                        actions=[act("2026-11-09", "bonus", "Bonus 1:1", True)])
    summary = ("Acme Industries Limited (ACME) closed at ₹50.50 on 9 Nov 2026; the previous close was ₹101.00 "
               "(unadjusted).")
    ans = bot([FP("groq", WHY, [summary])], store=store, nse=FakeNse(), news=FakeNews()).ask("why did acme fall")
    assert ans.writer == "llm:groq" and ans.text.count("Bonus 1:1") == 1 and "Possible contributing" not in ans.text
    assert "%" not in ans.text.split("\n\n")[0].split("Market that day")[0]                  # no raw -50% is ever shown


# ================================================================ robustness
def test_follow_up_questions_reuse_the_previous_company():
    first = bot([FP("groq", WHY, [SUMMARY])]).ask("why is acme up today")
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
    assert "secret" not in ans.text and ans.context == Context() and isinstance(ans, Answer)


def test_the_footer_lists_only_what_the_message_showed():
    nse = FakeNse(anns=[ann(f"2026-11-09T0{i}:30:00", f"Category {i}") for i in range(1, 7)],
                  meetings=[BoardMeeting("ACME", "2026-11-09", "Financial Results", "d", None, True)])
    for i, a in enumerate(nse.anns):                                      # give every filing its own link
        object.__setattr__(a, "url", f"https://nsearchives.nseindia.com/f{i}.pdf")
    ans = bot([FP("groq", WHY, [SUMMARY])], store=build_store(actions=DIVIDEND), nse=nse, news=FakeNews()).ask("why is acme up")
    body, footer = ans.text.split("\n\n", 1)
    shown_links = [l for l in footer.splitlines() if l.startswith("Link: ")]
    assert 1 <= len(shown_links) <= 3
    for line in shown_links:
        label = line[len("Link: "):].rsplit(" https://", 1)[0]
        assert label in body                                              # every linked item is in the message
    assert "(plus " in body and body.count("- NSE filing") < 6


def test_the_telegram_version_escapes_the_body_and_uses_html_links():
    ans = bot([FP("groq", WHY, [SUMMARY])]).ask("why is acme up today")
    body, footer = ans.telegram_html.split("\n\n", 1)[0], ans.telegram_html.rsplit("Sources: ", 1)[1]
    assert '<a href="https://news/1">' in footer and "<a href" not in ans.text          # links only in the HTML version
    assert "<a href" not in body and ans.body is not None and ans.text.startswith(ans.body)
    assert ans.footer_html and ans.telegram_html.endswith(ans.footer_html)
    fixed = bot([FP("groq", ToolCall("smalltalk", {}))]).ask("hello")
    assert fixed.telegram_html == B.HELP.replace("&", "&amp;") and fixed.body is None
