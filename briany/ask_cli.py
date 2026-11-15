"""Ask the bot a question end to end on real data (public data only; no secret is printed).

  python -m briany.ask_cli --question "why is reliance up today"
  python -m briany.ask_cli --suite
"""
from __future__ import annotations

import argparse
import sys
import time

from briany.bot import Bot
from briany.evidence import Assembler
from briany.llm import Chain, GeminiClient, GroqClient
from briany.news import NewsClient
from briany.nse import NseClient
from briany.router import Context
from briany.symbols import Resolver

SUITE = [
    ("why is reliance up today", None),
    ("and last month?", "follow-up"),
    ("tcs 1 month return", None),
    ("any announcements from infosys", None),
    ("why did angel one fall on 26 feb", None),
    ("ril 6 month return", None),
    ("compare tcs and infosys", None),
]


def build_bot():
    from briany.config import Settings
    from briany.ingest import connect
    _client, store, _s = connect()
    settings = Settings.from_env()
    _day, instruments = store.get_instruments()
    if not instruments:
        sys.exit("no instrument list stored; run ingest with refresh_instruments = yes")
    resolver = Resolver(instruments)
    providers = []
    if settings.groq_api_key:
        providers.append(GroqClient(settings.groq_api_key, model=settings.groq_model))
    if settings.gemini_api_key:
        providers.append(GeminiClient(settings.gemini_api_key, model=settings.gemini_model))
    asm = Assembler(store, NseClient(min_interval=settings.nse_min_interval_seconds), NewsClient(), resolver)
    return Bot(asm, Chain(providers), resolver)


def show(question, ans):
    print(f"\n=== Q: {question}")
    print(ans.text)
    r = ans.route
    print("--- debug: route=" + (f"{r.tool} {r.args} via {r.source}" if r else "-") + f" | writer={ans.writer}"
          + f" | kind={ans.kind}")
    if ans.routed and ans.routed.attempts:
        print("--- debug: router attempts=" + str(list(ans.routed.attempts)))
    if ans.problems:
        print("--- debug: rejected model text because: " + "; ".join(ans.problems[:6]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--question")
    ap.add_argument("--suite", action="store_true")
    ap.add_argument("--pause", type=float, default=25.0, help="seconds between questions (Groq 8,000 tokens/minute)")
    a = ap.parse_args(argv)
    if not a.question and not a.suite:
        sys.exit("give --question or --suite")
    bot = build_bot()
    ctx = Context()
    for question, _note in ([(a.question, None)] if a.question else SUITE):
        ans = bot.ask(question, ctx)
        show(question, ans)
        ctx = ans.context
        if a.suite:
            time.sleep(a.pause)


if __name__ == "__main__":
    main()
