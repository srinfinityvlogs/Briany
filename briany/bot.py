# SPDX-License-Identifier: Apache-2.0
"""The whole path from a question to a message: route -> assemble -> write (or fall back) -> validate -> footer.

The model only routes and, for price-move packets, rephrases. Returns, filings lists, refusals,
clarifications and every unsupported question are answered by code. English only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape as _escape

from briany.answer import (SUMMARY_SYSTEM, build_footer, compose_move, fallback_text, premise_note,
                           summary_input, validate_summary, validate_text)
from briany.evidence import IST, Packet
from briany.llm import LlmError
from briany.router import Context, Route, Routed, route_question

WRITER_KINDS = ("move",)          # packet kinds whose wording the model may rephrase (all others are code-built)

HELP = ("I answer questions about one NSE-listed stock or ETF at a time:\n"
        "- Why it moved: 'why is Tata Steel up today?'\n"
        "- Price return: 'TCS 6 month return' (1 week, 1 month, 3 months, 6 months, 1 year or year to date)\n"
        "- Filings and dates: 'any announcements from Infosys?'\n"
        "Prices are NSE end-of-day closes, not live quotes. Information only, not investment advice.")
UNSUPPORTED = {
    "comparison": "I can't compare stocks yet. Ask about one stock at a time, for example 'why is TCS up today?'.",
    "advice": "I don't give buy, sell or hold advice. I can show what happened to a stock and what was announced, "
              "for example 'why is Reliance up today?'.",
    "prediction": "I can't predict prices. I can show what happened to a stock and what was announced.",
    "market_data": "Market-wide data (indices, FII/DII totals, bulk deals) isn't available yet. FII/DII figures do "
                   "appear in a stock's move answer for the latest day.",
    "period": "I can give 1 week, 1 month, 3 months, 6 months, 1 year or year-to-date returns.",
    "company_not_found": "I couldn't tell which company you mean. Please give the company name or NSE symbol.",
    "other": "I can answer questions about one NSE-listed stock: why it moved, its return over a period, or its "
             "latest filings and results dates.",
}
ERROR_TEXT = "Something went wrong while preparing that answer. Please try again in a moment."


SUMMARY_MAX_TOKENS = 2000        # output budget for the opening summary; reasoning models spend part of it thinking


@dataclass(frozen=True)
class Answer:
    text: str                       # final message, including the footer
    kind: str                       # move | return | events | clarify | fixed | error
    route: Route | None
    packet: Packet | None
    writer: str                     # llm:<provider> | fallback (model text unavailable or rejected) | code | fixed | error
    problems: tuple                 # validator problems from rejected model text (for logs)
    context: Context
    routed: Routed | None = None
    write_attempts: tuple = ()      # ((provider, outcome), ...) for the model-written summary
    body: str | None = None         # message without the footer (None = the whole text is the body)
    footer_html: str = ""           # footer with Telegram HTML links

    @property
    def telegram_html(self) -> str:
        """Body escaped for Telegram HTML, then the HTML footer (its links and text are built escaped)."""
        body = _escape(self.text if self.body is None else self.body, quote=False)
        return body + ("\n\n" + self.footer_html if self.footer_html else "")


class Bot:
    def __init__(self, assembler, chain, resolver, now=None):
        self.asm, self.chain, self.resolver = assembler, chain, resolver
        self._now = now or (lambda: datetime.now(IST))

    def _packet(self, route: Route) -> Packet:
        a = route.args
        if route.tool == "why_moved":
            return self.asm.why_moved(a["query"], on=a.get("date"))
        if route.tool == "period_return":
            return self.asm.period_return(a["query"], label=a["period"])
        return self.asm.events(a["query"])

    def _compose(self, packet: Packet, question: str = ""):
        notes, shown = premise_note(question, packet), []
        if packet.status != "ok" or packet.kind not in WRITER_KINDS:
            return fallback_text(packet, notes, shown), "code", [], (), shown   # code by design; no model involved
        problems = []

        def validate(text):
            found = validate_summary(text, packet)
            if found:
                problems.extend(found)
                raise ValueError("; ".join(found))
        attempts = ()
        try:
            summary, provider, attempts = self.chain.write(SUMMARY_SYSTEM, summary_input(packet), max_tokens=SUMMARY_MAX_TOKENS,
                                                            validate=validate)
            body = compose_move(packet, summary, notes, shown)
            final = validate_text(body, packet)                    # a last check on the assembled message
            if not final:
                return body, f"llm:{provider}", problems, tuple(attempts), shown
            problems.extend("assembled message: " + x for x in final)
        except LlmError as e:
            attempts = getattr(e, "attempts", ())
        shown = []
        return fallback_text(packet, notes, shown), "fallback", problems, tuple(attempts), shown

    def ask(self, question: str, context: Context | None = None) -> Answer:
        context = context or Context()
        today = self._now().astimezone(IST).date().isoformat()
        try:
            routed = route_question(question, self.chain, self.resolver, today, context)
            route = routed.route
            if route.tool == "smalltalk":
                return Answer(HELP, "fixed", route, None, "fixed", (), context, routed)
            if route.tool == "unsupported":
                text = UNSUPPORTED.get(route.args.get("reason"), UNSUPPORTED["other"])
                return Answer(text, "fixed", route, None, "fixed", (), context, routed)
            packet = self._packet(route)
            text, writer, problems, w_attempts, shown = self._compose(packet, question)
            footer = "" if packet.status == "clarify" else build_footer(packet, factors=shown)
            footer_html = "" if packet.status == "clarify" else build_footer(packet, factors=shown, html_links=True)
            new_ctx = Context(packet.symbol, packet.name) if packet.symbol and packet.status != "clarify" else context
            return Answer(text + ("\n\n" + footer if footer else ""), packet.kind, route, packet, writer,
                          tuple(problems), new_ctx, routed, w_attempts, body=text, footer_html=footer_html)
        except Exception as e:  # noqa: BLE001 - the bot must always answer; log the type only (no secrets)
            return Answer(ERROR_TEXT, "error", None, None, "error", (type(e).__name__,), context)
