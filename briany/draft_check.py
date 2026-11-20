# SPDX-License-Identifier: Apache-2.0
"""Why does the validator reject some model drafts? Run each provider on real packets and print what it wrote.

  python -m briany.draft_check --max 9 --live "why is reliance up today"
Packets come from the recorded Phase 1 scenarios (offline) and, with --live, from real data. Each provider writes the
opening summary exactly as the bot asks it to; the same validator the bot uses then judges the draft. Rejected drafts are
printed in full with the reasons, so the prompt or the validator can be fixed from evidence. Public data only; keys are
read from the environment and never printed. This is a developer tool: the live bot never logs a model's text.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

from briany.answer import SUMMARY_SYSTEM, summary_input, validate_summary
from briany.bot import SUMMARY_MAX_TOKENS
from briany.llm import LlmError

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "phase1" / "phase1_scenarios.json.gz"


def fixture_packets(path=FIXTURE):
    """[(id, Packet)] for every recorded 'why did it move' scenario that has data."""
    from briany.snapshot import load_snapshot, replay_assembler, run_call
    snap = load_snapshot(path)
    asm, _now = replay_assembler(snap)
    out = []
    for r in snap["results"]:
        if r["call"]["fn"] != "why_moved":
            continue
        packet = run_call(asm, r["call"])
        if packet.kind == "move" and packet.status == "ok":
            out.append((r["id"], packet))
    return out


def live_packets(questions):
    """[(id, Packet)] for real questions on real data (needs Redis and NSE access)."""
    from datetime import datetime
    from briany.ask_cli import build_bot
    from briany.evidence import IST
    from briany.router import Context, route_question
    bot = build_bot()
    today = datetime.now(IST).date().isoformat()
    out = []
    for q in questions:
        route = route_question(q, bot.chain, bot.resolver, today, Context()).route
        if route.tool != "why_moved":
            print(f"skipping {q!r}: it routes to {route.tool}, not a move question")
            continue
        out.append((f"live:{q}", bot._packet(route)))
    return out


def pick(items, n):
    """n items spread evenly over the list (not just the first stock's three days)."""
    if n <= 0 or n >= len(items):
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def evaluate(provider, packets, pause=15.0, sleep=time.sleep, max_tokens=SUMMARY_MAX_TOKENS):
    """One row per packet: {'id', 'draft', 'problems', 'error'}. A rate limit waits a minute and tries once more."""
    rows = []
    for i, (id_, packet) in enumerate(packets):
        row = {"id": id_, "draft": None, "problems": [], "error": None, "detail": ""}
        for attempt in (1, 2):
            try:
                row["draft"] = provider.write(SUMMARY_SYSTEM, summary_input(packet), max_tokens)
                row["problems"] = validate_summary(row["draft"], packet)
                row["error"] = None
                break
            except LlmError as e:
                row["error"], row["detail"] = e.kind, str(e).removeprefix(e.kind).lstrip(": ")[:150]
                if e.kind in ("rate_limit",) and attempt == 1:
                    sleep(60)
                    continue
                break
        rows.append(row)
        if i < len(packets) - 1:
            sleep(pause)
    return rows


def category(problem: str) -> str:
    """'number not in the data: 202' -> 'number not in the data'; digits and quoted words are dropped so counts group."""
    return re.sub(r"\d+", "N", problem.split(":")[0]).strip()


def report(results: dict) -> str:
    """results: {provider name: rows}. Plain text, rejected drafts in full."""
    lines = []
    for name, rows in results.items():
        accepted = [r for r in rows if r["draft"] is not None and not r["problems"] and not r["error"]]
        rejected = [r for r in rows if r["draft"] is not None and r["problems"]]
        errors = [r for r in rows if r["error"]]
        lines.append(f"===== {name}: {len(accepted)} accepted, {len(rejected)} rejected, {len(errors)} errors "
                     f"of {len(rows)} packets")
        counts = Counter(category(p) for r in rejected for p in r["problems"])
        if counts:
            lines.append("  why drafts were rejected: " + "; ".join(f"{k} x{v}" for k, v in counts.most_common()))
        if errors:
            lines.append("  provider errors: " + "; ".join(f"{r['id']} ({r['error']}: {r['detail']})" if r["detail"] else f"{r['id']} ({r['error']})" for r in errors))
        for r in rejected:
            lines.append(f"\n  REJECTED {r['id']}\n    problems: " + " | ".join(r["problems"]) + f"\n    draft: {r['draft']!r}")
        for r in accepted[:2]:
            lines.append(f"\n  ACCEPTED (example) {r['id']}\n    draft: {r['draft']!r}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def providers_from_env(only=None):
    from briany.config import Settings
    from briany.llm import GeminiClient, GroqClient
    s = Settings.from_env()
    found = []
    if s.groq_api_key:
        found.append(GroqClient(s.groq_api_key, model=s.groq_model))
    if s.gemini_api_key:
        found.append(GeminiClient(s.gemini_api_key, model=s.gemini_model))
    return [p for p in found if not only or p.name in only]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=9, help="how many recorded packets to try, spread over stocks and days")
    ap.add_argument("--live", action="append", default=[], help="also try a real question on real data (repeatable)")
    ap.add_argument("--providers", default="", help="comma list, e.g. groq (default: every provider with a key)")
    ap.add_argument("--pause", type=float, default=15.0, help="seconds between calls (Groq allows about 8,000 tokens a minute)")
    ap.add_argument("--out", default="drafts")
    ap.add_argument("--max-tokens", type=int, default=SUMMARY_MAX_TOKENS, help="output budget per draft (default: the bot's)")
    ap.add_argument("--repeat", type=int, default=1, help="run the live questions this many times (models vary run to run)")
    a = ap.parse_args(argv)
    providers = providers_from_env({p.strip() for p in a.providers.split(",") if p.strip()})
    if not providers:
        sys.exit("no model key found: set GROQ_API_KEY and/or GEMINI_API_KEY")
    packets = pick(fixture_packets(), a.max) + (live_packets([q for q in a.live if q.strip()]) * max(1, a.repeat) if any(a.live) else [])
    print(f"{len(packets)} packets, providers: {', '.join(p.name for p in providers)}")
    results = {p.name: evaluate(p, packets, a.pause, max_tokens=a.max_tokens) for p in providers}
    text = report(results)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
