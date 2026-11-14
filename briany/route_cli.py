"""Try the question router on real Groq and Gemini (needs GROQ_API_KEY / GEMINI_API_KEY) and on the rules.

  python -m briany.route_cli --suite
  python -m briany.route_cli --question "why did reliance fall on 5 nov"
Prints routing decisions only; no secret is ever printed.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

from briany.llm import GeminiClient, GroqClient, LlmError
from briany.router import SYSTEM, TOOLS, Context, rule_route, validate_route


def build_suite(today: str):
    """(question, expected, context). expected keys: tool, query_has, period, date, reason."""
    t = Date.fromisoformat(today)
    yday = (t - timedelta(days=1)).isoformat()
    nov5 = Date(t.year if t >= Date(t.year, 11, 5) else t.year - 1, 11, 5).isoformat()
    rel, tcs = Context("RELIANCE", "Reliance Industries Limited"), Context("TCS", "Tata Consultancy Services Limited")
    W, R, E, U = "why_moved", "period_return", "events", "unsupported"
    return [
        ("why is tata steel up today", {"tool": W, "query_has": "tata steel"}, None),
        ("Why did Reliance fall on 5 Nov?", {"tool": W, "query_has": "reliance", "date": nov5}, None),
        ("what happened to TCS yesterday", {"tool": W, "query_has": "tcs", "date": yday}, None),
        ("infosys is crashing, what's going on", {"tool": W, "query_has": "infosys"}, None),
        ("TCS 6 month return", {"tool": R, "query_has": "tcs", "period": "6m"}, None),
        ("how has HDFC Bank done over the last year", {"tool": R, "query_has": "hdfc", "period": "1y"}, None),
        ("angel one returns year to date", {"tool": R, "query_has": "angel", "period": "ytd"}, None),
        ("what's the gain in reliance since last week", {"tool": R, "query_has": "reliance", "period": "1w"}, None),
        ("reliance 2 year return", {"tool": U, "reason": "period"}, None),
        ("when are Reliance's next results", {"tool": E, "query_has": "reliance"}, None),
        ("any announcements from Angel One recently", {"tool": E, "query_has": "angel"}, None),
        ("compare TCS and Infosys", {"tool": U, "reason": "comparison"}, None),
        ("which is better, tata steel or tata power", {"tool": U, "reason": "comparison"}, None),
        ("should I buy Reliance now", {"tool": U, "reason": "advice"}, None),
        ("will Reliance hit 1500 next week", {"tool": U, "reason": "prediction"}, None),
        ("what are FIIs doing today", {"tool": U, "reason": "market_data"}, None),
        ("how is the nifty doing", {"tool": U, "reason": "market_data"}, None),
        ("hi", {"tool": "smalltalk"}, None),
        ("what can you do", {"tool": "smalltalk"}, None),
        ("why is relience up", {"tool": W, "query_has": "relience"}, None),
        ("ril share price move today", {"tool": W}, None),
        ("tell me about the weather in Mumbai", {"tool": U}, None),
        ("and last month?", {"tool": R, "query_has": "reliance", "period": "1m"}, rel),
        ("why did it fall?", {"tool": W, "query_has": "tcs"}, tcs),
        ("Reliance kyun gira 5 nov ko", {"tool": W, "query_has": "reliance", "date": nov5}, None),
    ]


def matches(expected: dict, route) -> bool:
    if route is None or route.tool != expected["tool"]:
        return False
    a = route.args
    if "query_has" in expected and expected["query_has"] not in str(a.get("query", "")).lower():
        return False
    return all(a.get(k) == expected[k] for k in ("period", "date", "reason") if k in expected)


def short(route) -> str:
    if route is None:
        return "-"
    parts = [route.tool] + [f"{k}={v}" for k, v in route.args.items()]
    return " ".join(parts)


def run_provider(client, question, today, context):
    ctx_line = (f"Previous company in this conversation: {context.last_symbol} "
                "(for a follow-up, use exactly this symbol as the query)." if context
                else "No previous company in this conversation.")
    system = SYSTEM.format(today=today, context=ctx_line)
    try:
        call = client.route(system, question, TOOLS)
        return validate_route(call.name, call.args, today, client.name), None
    except LlmError as e:
        return None, (e.kind, str(e))
    except ValueError as e:
        return None, ("invalid_route", f"invalid_route: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--question")
    ap.add_argument("--suite", action="store_true")
    ap.add_argument("--providers", default="groq,gemini")
    ap.add_argument("--pause-groq", type=float, default=8.0, help="seconds between Groq calls (8,000 tokens/minute)")
    ap.add_argument("--pause-gemini", type=float, default=10.0)
    a = ap.parse_args(argv)
    if not a.question and not a.suite:
        sys.exit("give --question or --suite")

    from briany.config import Settings
    from briany.ingest import connect
    _client, store, _settings = connect()
    settings = Settings.from_env()
    _day, instruments = store.get_instruments()
    if not instruments:
        sys.exit("no instrument list stored; run ingest with refresh_instruments = yes")
    from briany.symbols import Resolver
    resolver = Resolver(instruments)

    clients = {}
    wanted = [p.strip() for p in a.providers.split(",") if p.strip()]
    if "groq" in wanted:
        if not settings.groq_api_key:
            sys.exit("GROQ_API_KEY is not set")
        clients["groq"] = (GroqClient(settings.groq_api_key, model=settings.groq_model), a.pause_groq)
    if "gemini" in wanted:
        if not settings.gemini_api_key:
            sys.exit("GEMINI_API_KEY is not set")
        clients["gemini"] = (GeminiClient(settings.gemini_api_key, model=settings.gemini_model), a.pause_gemini)

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    cases = build_suite(today) if a.suite else [(a.question, {"tool": None}, None)]
    score = {name: 0 for name in list(clients) + ["rules"]}
    errors = {name: {} for name in clients}
    for question, expected, ctx in cases:
        row = {}
        for name, (client, pause) in clients.items():
            route, err = run_provider(client, question, today, ctx)
            if err:
                errors[name][err[0]] = errors[name].get(err[0], 0) + 1
            row[name] = (route, err)
            time.sleep(pause)
        row["rules"] = (rule_route(question, resolver, today, ctx), None)
        print(f"\nQ: {question}" + (f"   [context: {ctx.last_symbol}]" if ctx else ""))
        if a.suite:
            print(f"   expected: {expected}")
        for name, (route, err) in row.items():
            ok = a.suite and matches(expected, route)
            score[name] += 1 if ok else 0
            print(f"   {name:7} {'OK ' if ok else ('-- ' if not a.suite else 'XX ')}{short(route) if route else 'ERROR ' + (err[1] if err else '')}")
    if a.suite:
        print(f"\n=== score out of {len(cases)} ===")
        for name, (client, _p) in clients.items():
            if getattr(client, "model", ""):
                print(f"{name} model: {client.model}")
        for name, n in score.items():
            print(f"{name:7} {n}/{len(cases)}" + (f"   errors: {errors[name]}" if errors.get(name) else ""))


if __name__ == "__main__":
    main()
