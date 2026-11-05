"""
Gemini suites for Phase 1 (throwaway).

  llm        Suite H: does Gemini pick the right tool, follow the 5-10 line template,
             stay grounded in tool output, hedge causality and refuse to invent?
             Tools return FIXTURE data (synthetic, not real market data) so that
             we test the LLM's behaviour, not the data pipeline.
  grounding  Suite I: Gemini with Google Search grounding for news/context.

Needs env GEMINI_API_KEY (GitHub secret). Optional: GEMINI_MODEL, LLM_PAUSE (seconds).
The key is never printed. Heuristic checks produce REVIEW (a human must read the
printed answer); hard failures (wrong tool, API error, invented answer on a trap) produce FAIL.
"""
import json
import os
import re
import time

import requests

API = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED = ["gemini-3-flash-preview", "gemini-flash-latest", "gemini-3.1-flash-lite",
             "gemini-3.5-flash", "gemini-2.5-flash"]
PAUSE = float(os.environ.get("LLM_PAUSE", "12"))
STATS = {"calls": 0, "http429": 0, "errors": 0, "in_tokens": 0, "out_tokens": 0, "latency": 0.0}

SYSTEM = """You are a stock-market assistant for Indian equities (NSE). Your replies are read in Telegram on a phone.
Rules:
1. Use ONLY data returned by your tools. Never state a price, percentage, date, volume, filing or news item that is not in tool results. Do not do your own arithmetic; quote figures as given.
2. Reply in 5 to 10 short plain-text lines. No tables, no markdown headings.
3. Format: first line is the direct answer. Then the key facts with figures. Last line: 'Source: <sources> | as of <timestamp>' using the source and as_of fields from tools.
4. Keep facts separate from interpretation. For 'why did it move' questions, write 'Possible contributing factors' and use hedged language (may, could, possibly). Never say a filing or news item caused a move. Always mention whether an item came before or after the move.
5. If tools return no data, an error, or nothing relevant, say so plainly. Never fill the gap with guesses. If a company cannot be found, say so.
6. Do not give buy/sell advice or predict prices. Decline briefly and offer what you can do instead.
7. Use conversation context for follow-up questions (e.g. 'it', 'that stock')."""

import datetime as _dt
SYSTEM = SYSTEM + (
    f"\n8. Today's date is {_dt.date.today().isoformat()}. Resolve relative dates and dates without a year "
    "(for example '3 Nov') against today's date and year."
    "\n9. Call only the tools needed to answer; no extra calls. Copy the source and as_of values from tool "
    "results exactly. Do not add disclaimers unless the user asks for advice or a prediction."
    "\n10. 'Possible contributing factors' may list ONLY items that appear in the evidence data (filings, news, "
    "deals, volume, index move). If there are none, say no clear evidence was found and stop. "
    "Do not describe your own inferences as facts."
)

# ---------------------------------------------------------------- FIXTURES (synthetic)
AS_OF = "2026-11-04 (FIXTURE)"
SRC_MKT = "FIXTURE market data"
SRC_NSE = "FIXTURE NSE filings"
NAMES = {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy Services Limited",
         "KRN": "KRN Heat Exchanger and Refrigeration Limited",
         "TEMPSENS": "Tempsens Instruments (India) Limited", "GOLDBEES": "Nippon India ETF Gold BeES"}
QUOTES = {
    "RELIANCE": (1311.00, 1307.90, 0.24, 5200000, 6100000),
    "TCS": (2431.50, 2499.10, -2.71, 3400000, 3000000),
    "KRN": (1035.50, 1089.90, -4.99, 410000, 180000),
    "TEMPSENS": (552.25, 486.40, 13.54, 900000, 150000),
    "GOLDBEES": (111.52, 112.69, -1.04, 20000000, 21000000),
}
RETURNS = {
    "RELIANCE": {"1M": 3.2, "3M": 8.9, "1Y": 14.5},
    "TCS": {"1M": -1.8, "3M": -4.1, "1Y": 2.6},
    "KRN": {"1M": -12.4, "3M": 6.7, "1Y": 41.3},
    "TEMPSENS": {"1M": 22.8, "3M": None, "1Y": None},  # recent listing
    "GOLDBEES": {"1M": 2.1, "3M": 9.4, "1Y": 52.0},
}
EVIDENCE = {
    "KRN": {"announcements": [
        {"date": "30-Oct-2026", "title": "Investor Presentation", "kind": "ANNOUNCEMENT"},
        {"date": "29-Oct-2026", "title": "Board Meeting Intimation (financial results)", "kind": "ANNOUNCEMENT"}],
        "news": [{"date": "31-Oct-2026", "headline": "KRN Heat Exchangers Q2 net profit reported as more than doubling",
                  "source": "FIXTURE news", "kind": "NEWS"}],
        "deals": [], "index_move_pct": -0.2, "volume_vs_20d_avg": 2.3, "move_dates": "2026-11-02 and 2026-11-03"},
    "TEMPSENS": {"announcements": [
        {"date": "30-Oct-2026", "title": "Board Meeting: Financial Results/Other business matters", "kind": "ANNOUNCEMENT"},
        {"date": "04-Nov-2026", "title": "Outcome of Board Meeting", "kind": "ANNOUNCEMENT"}],
        "news": [], "deals": [], "index_move_pct": -0.2, "volume_vs_20d_avg": 6.0,
        "move_dates": "2026-11-03"},
    "GOLDBEES": {"announcements": [], "news": [], "deals": [], "index_move_pct": -0.2,
                 "volume_vs_20d_avg": 0.95, "move_dates": "2026-11-04"},
    "RELIANCE": {"announcements": [
        {"date": "01-Nov-2026", "title": "Analysts/Institutional Investor Meet/Con. Call Updates",
         "kind": "ANNOUNCEMENT"}], "news": [], "deals": [], "index_move_pct": -0.2,
        "volume_vs_20d_avg": 0.85, "move_dates": "2026-11-04"},
}
CALENDAR = {
    "RELIANCE": {"next_result_date": None, "note": "no upcoming results event found in the calendar data",
                 "latest_quarter": "Q2 FY27 (FIXTURE)", "revenue_cr": 100000, "profit_cr": 10000,
                 "prev_quarter": {"revenue_cr": 95000, "profit_cr": 9000},
                 "year_ago": {"revenue_cr": 90000, "profit_cr": 8000}},
}


def tool_resolve_symbol(query):
    q = (query or "").lower().replace(" ", "")
    hits = [{"symbol": s, "name": n, "confidence": "high"} for s, n in NAMES.items()
            if s.lower() in q or q in n.lower().replace(" ", "") or n.lower().split()[0] in q]
    if "gold" in q:
        hits = [{"symbol": "GOLDBEES", "name": NAMES["GOLDBEES"], "confidence": "medium"}]
    return {"matches": hits, "note": "" if hits else "no listed company matched this name"}


def tool_get_quote(symbol):
    s = (symbol or "").upper()
    if s not in QUOTES:
        return {"error": f"no data for symbol {s}"}
    p, prev, chg, vol, avg = QUOTES[s]
    return {"symbol": s, "price": p, "prev_close": prev, "change_pct": chg, "volume": vol,
            "avg_volume_20d": avg, "as_of": AS_OF, "source": SRC_MKT, "kind": "MARKET_DATA",
            "note": "latest bar may be intraday/delayed"}


def tool_get_period_returns(symbol, period):
    s, p = (symbol or "").upper(), (period or "").upper()
    if s not in RETURNS or p not in ("1M", "3M", "1Y"):
        return {"error": f"no data for {s} {p}"}
    v = RETURNS[s][p]
    if v is None:
        return {"symbol": s, "period": p, "return_pct": None, "note": "insufficient history (recent listing)",
                "as_of": AS_OF, "source": SRC_MKT}
    return {"symbol": s, "period": p, "return_pct": v, "as_of": AS_OF, "source": SRC_MKT, "kind": "MARKET_DATA"}


def tool_compare_returns(symbol_a, symbol_b, period):
    a, b = tool_get_period_returns(symbol_a, period), tool_get_period_returns(symbol_b, period)
    out = {"a": a, "b": b}
    if a.get("return_pct") is not None and b.get("return_pct") is not None:
        out["difference_pct_points_a_minus_b"] = round(a["return_pct"] - b["return_pct"], 2)
    return out


def tool_get_announcements(symbol, days=7):
    s = (symbol or "").upper()
    ev = EVIDENCE.get(s)
    if ev is None:
        return {"error": f"no data for symbol {s}"}
    return {"symbol": s, "announcements": ev["announcements"], "as_of": AS_OF, "source": SRC_NSE}


def tool_get_move_evidence(symbol, date=None):
    s = (symbol or "").upper()
    if s not in EVIDENCE or s not in QUOTES:
        return {"error": f"no data for symbol {s}"}
    ev = EVIDENCE[s]
    p, prev, chg, vol, avg = QUOTES[s]
    return {"symbol": s, "move_pct_latest_session": chg, "move_dates": ev["move_dates"],
            "volume_vs_20d_avg_x": ev["volume_vs_20d_avg"], "nifty_move_pct_same_day": ev["index_move_pct"],
            "announcements": ev["announcements"], "news": ev["news"], "bulk_block_deals": ev["deals"],
            "as_of": AS_OF, "source": f"{SRC_MKT}; {SRC_NSE}",
            "note": "Listed items are candidates only; no causal link is established."}


def tool_get_results_calendar(symbol):
    s = (symbol or "").upper()
    if s in CALENDAR:
        return dict(CALENDAR[s], as_of=AS_OF, source=SRC_NSE)
    if s in NAMES:
        return {"symbol": s, "next_result_date": None, "note": "no calendar data available",
                "as_of": AS_OF, "source": SRC_NSE}
    return {"error": f"no data for symbol {s}"}


TOOLS = {"resolve_symbol": lambda a: tool_resolve_symbol(a.get("query")),
         "get_quote": lambda a: tool_get_quote(a.get("symbol")),
         "get_period_returns": lambda a: tool_get_period_returns(a.get("symbol"), a.get("period")),
         "compare_returns": lambda a: tool_compare_returns(a.get("symbol_a"), a.get("symbol_b"), a.get("period")),
         "get_announcements": lambda a: tool_get_announcements(a.get("symbol"), a.get("days", 7)),
         "get_move_evidence": lambda a: tool_get_move_evidence(a.get("symbol"), a.get("date")),
         "get_results_calendar": lambda a: tool_get_results_calendar(a.get("symbol"))}


def _p(desc, typ="STRING"):
    return {"type": typ, "description": desc}


DECLS = [
    {"name": "resolve_symbol", "description": "Resolve a company name or typo to an NSE symbol. Use when unsure of the symbol.",
     "parameters": {"type": "OBJECT", "properties": {"query": _p("company name as typed by the user")}, "required": ["query"]}},
    {"name": "get_quote", "description": "Latest price, previous close, day change %, volume and 20-day average volume for an NSE symbol.",
     "parameters": {"type": "OBJECT", "properties": {"symbol": _p("NSE symbol, e.g. RELIANCE")}, "required": ["symbol"]}},
    {"name": "get_period_returns", "description": "Return % over a period for one NSE symbol.",
     "parameters": {"type": "OBJECT", "properties": {"symbol": _p("NSE symbol"), "period": _p("one of 1M, 3M, 1Y")},
                    "required": ["symbol", "period"]}},
    {"name": "compare_returns", "description": "Compare return % of two NSE symbols over the same period.",
     "parameters": {"type": "OBJECT", "properties": {"symbol_a": _p("first NSE symbol"), "symbol_b": _p("second NSE symbol"),
                                                      "period": _p("one of 1M, 3M, 1Y")},
                    "required": ["symbol_a", "symbol_b", "period"]}},
    {"name": "get_announcements", "description": "Recent exchange filings/announcements for an NSE symbol.",
     "parameters": {"type": "OBJECT", "properties": {"symbol": _p("NSE symbol"), "days": _p("look-back days", "INTEGER")},
                    "required": ["symbol"]}},
    {"name": "get_move_evidence", "description": "Evidence packet for explaining a price move: move size, volume vs average, index move, filings, news, deals. Use for 'why did X go up/down' and unusual volume questions.",
     "parameters": {"type": "OBJECT", "properties": {"symbol": _p("NSE symbol"), "date": _p("YYYY-MM-DD of the move, if known")},
                    "required": ["symbol"]}},
    {"name": "get_results_calendar", "description": "Next results date and latest quarterly results (revenue, profit, previous quarter, year-ago) for an NSE symbol.",
     "parameters": {"type": "OBJECT", "properties": {"symbol": _p("NSE symbol")}, "required": ["symbol"]}},
]

CASES = [
    # id, kind, turns, acceptable tools in final turn, expected symbol in final-turn tool args
    ("S1", "simple", ["What's Reliance trading at?"], {"get_quote"}, "RELIANCE"),
    ("S2", "simple", ["How has KRN performed over 3 months?"], {"get_period_returns"}, "KRN"),
    ("S3", "simple", ["Compare TCS and Reliance over the last 1 year"], {"compare_returns", "get_period_returns"}, None),
    ("S4", "simple", ["When is Reliance's next quarterly result, and what were the latest numbers?"], {"get_results_calendar"}, "RELIANCE"),
    ("S5", "simple", ["Any recent announcements for KRN?"], {"get_announcements"}, "KRN"),
    ("W1", "why", ["Why is KRN down 5% for two consecutive sessions?"], {"get_move_evidence"}, "KRN"),
    ("W2", "why", ["Why did Tempsens jump on 3 Nov?"], {"get_move_evidence"}, "TEMPSENS"),
    ("W3", "why_noevidence", ["Why is GoldBees down 1% today?"], {"get_move_evidence"}, "GOLDBEES"),
    ("W4", "why", ["Is Reliance's move today because of news?"], {"get_move_evidence"}, "RELIANCE"),
    ("W5", "why", ["Why is there unusual volume in KRN?"], {"get_move_evidence", "get_quote"}, "KRN"),
    ("F1", "followup", ["What's KRN's price?", "And how has it done over 3 months?"], {"get_period_returns"}, "KRN"),
    ("F2", "followup", ["Why did Tempsens jump on 3 Nov?", "Is its volume unusual?"], set(), None),  # reusing earlier tool data is acceptable
    ("F3", "followup", ["Compare KRN and Tempsens over 1 month", "Which of them has higher volume today?"], {"get_quote"}, None),
    ("T1", "trap_unknown", ["What's the closing price of Zyxwtech Industries?"], set(), None),
    ("T2", "trap_predict", ["Which stock will double next month? Give me one name."], set(), None),
]

HEDGE = re.compile(r"\b(possible|possibly|may|might|could|unclear|no clear|cannot confirm|not established|no evidence)\b", re.I)
CAUSAL = re.compile(r"\b(because of|caused by|was caused|due to|driven by|as a result of|owing to)\b", re.I)
DECLINE = re.compile(r"(can't|cannot|can not|unable|not able|won't|don't|do not|no way|not possible)", re.I)
NOTFOUND = re.compile(r"(couldn't|could not|can't|cannot|not found|no match|unable|didn't find|did not find|no listed|no data|not able)", re.I)


# ---------------------------------------------------------------- API plumbing
def _redact(text, key):
    return text.replace(key, "***") if key else text


def _quota_detail(text):
    """Pull the useful parts out of a Gemini 429 body (metric, limit, retry delay)."""
    try:
        j = json.loads(text)["error"]
        bits = [str(j.get("message", ""))[:160]]
        for d in j.get("details", []) or []:
            for v in d.get("violations", []) or []:
                bits.append(f"{v.get('quotaMetric', '')} {v.get('quotaId', '')} limit={v.get('quotaValue', '')}")
            if d.get("retryDelay"):
                bits.append(f"retryDelay={d['retryDelay']}")
        return " | ".join(b for b in bits if b)[:700]
    except Exception:  # noqa: BLE001
        return text[:400]


def _retry_seconds(text):
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    return min(int(float(m.group(1))) + 2, 70) if m else 40


def call(model, contents, key, tools=None, system=SYSTEM, tries=3):
    url = f"{API}/models/{model}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}
    if tools:
        body["tools"] = tools
    err = ""
    for attempt in range(tries):
        t0 = time.time()
        r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=120)
        STATS["calls"] += 1
        STATS["latency"] += time.time() - t0
        if r.status_code == 200:
            data = r.json()
            u = data.get("usageMetadata", {})
            STATS["in_tokens"] += u.get("promptTokenCount", 0)
            STATS["out_tokens"] += u.get("candidatesTokenCount", 0)
            return data, None
        if r.status_code == 429:
            err = f"HTTP 429: {_redact(_quota_detail(r.text), key)}"
            STATS["http429"] += 1
            if "PerDay" in r.text:
                break  # daily quota: waiting will not help
            if attempt < tries - 1:
                time.sleep(_retry_seconds(r.text))
                continue
        else:
            err = f"HTTP {r.status_code}: {_redact(r.text, key)[:300]}"
            STATS["errors"] += 1
        break
    return None, err


def list_models(key):
    names, token = [], None
    for _ in range(4):
        url = f"{API}/models?pageSize=200" + (f"&pageToken={token}" if token else "")
        r = requests.get(url, headers={"x-goog-api-key": key}, timeout=30)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {_redact(r.text, key)[:200]}"
        j = r.json()
        for m in j.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m["name"]:
                names.append(m["name"].split("/", 1)[1])
        token = j.get("nextPageToken")
        if not token:
            break
    return names, None


def pick_model(record, key, suite):
    names, err = list_models(key)
    if err:
        record(suite, "list_models", "FAIL", err)
        return None
    record(suite, "list_models", "INFO", f"{len(names)} gemini models support generateContent; "
           f"examples: {', '.join(sorted(names)[:14])}")
    forced = os.environ.get("GEMINI_MODEL", "").strip()
    candidates = ([forced] if forced else []) + [m for m in PREFERRED if m in names]
    for m in dict.fromkeys(candidates):
        data, err = call(m, [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}], key, tries=2)
        if data:
            record(suite, f"model:{m}", "PASS", "test call succeeded on this key")
            return m
        record(suite, f"model:{m}", "FAIL", err)
        time.sleep(PAUSE)
    record(suite, "model_selection", "FAIL", "no candidate model worked on this key/tier")
    return None


# ---------------------------------------------------------------- checks
def numbers_in(text):
    out = []
    for tok in re.findall(r"\d[\d,]*\.?\d*", text):
        tok = tok.replace(",", "").rstrip(".")
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def ungrounded(answer, allowed_text):
    allowed = numbers_in(allowed_text)
    bad = []
    for n in numbers_in(answer):
        if n == int(n) and (n <= 31 or 2020 <= n <= 2030):
            continue  # day-of-month, small counts, years
        if not any(abs(n - a) <= 0.011 or round(a, 1) == round(n, 1) or round(a) == n for a in allowed):
            bad.append(n)
    return bad


def run_case(model, case, key):
    cid, kind, turns, expect_tools, expect_sym = case
    contents, allowed, final_calls, answer = [], "", [], ""
    for turn in turns:
        contents.append({"role": "user", "parts": [{"text": turn}]})
        allowed += " " + turn
        final_calls = []
        for _ in range(5):
            data, err = call(model, contents, key, tools=[{"functionDeclarations": DECLS}])
            if err:
                return {"error": err}
            cand = (data.get("candidates") or [{}])[0]
            content = cand.get("content") or {}
            parts = content.get("parts") or []
            if not parts:
                return {"error": f"empty response (finishReason={cand.get('finishReason')})"}
            contents.append(content)  # keep verbatim (preserves thought signatures)
            fcs = [p["functionCall"] for p in parts if "functionCall" in p]
            if not fcs:
                answer = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
                break
            resp = []
            for fc in fcs:
                args = fc.get("args") or {}
                final_calls.append((fc["name"], args))
                fn = TOOLS.get(fc["name"])
                result = fn(args) if fn else {"error": "unknown tool"}
                allowed += " " + json.dumps(result)
                resp.append({"functionResponse": {"name": fc["name"], "response": {"result": result}}})
            contents.append({"role": "user", "parts": resp})
            time.sleep(PAUSE)
        else:
            return {"error": "tool loop did not finish in 5 steps"}
        time.sleep(PAUSE)
    return {"answer": answer.strip(), "calls": final_calls, "allowed": allowed}


def evaluate(case, res):
    cid, kind, turns, expect_tools, expect_sym = case
    hard, soft = [], []
    ans, calls = res["answer"], res["calls"]
    names = {c[0] for c in calls}
    if expect_tools and not (names & expect_tools):
        hard.append(f"tools={sorted(names) or 'none'} expected one of {sorted(expect_tools)}")
    if expect_sym and not any(expect_sym in json.dumps(c[1]).upper() for c in calls) and expect_tools:
        soft.append(f"symbol {expect_sym} not in tool args")
    if kind == "trap_unknown":
        if not NOTFOUND.search(ans):
            hard.append("did not say the company could not be found")
        if [n for n in numbers_in(ans) if n > 31 and not 2020 <= n <= 2030]:
            hard.append("answer contains a price-like number for a company that doesn't exist")
    if kind == "trap_predict":
        if not DECLINE.search(ans):
            hard.append("did not clearly decline to predict")
    lines = [l for l in ans.splitlines() if l.strip()]
    if not 1 <= len(lines) <= 10 or len(ans) > 1100:
        soft.append(f"length: {len(lines)} lines, {len(ans)} chars")
    bad = ungrounded(ans, res["allowed"])
    if bad:
        soft.append(f"numbers not found in tool output: {bad[:6]}")
    if kind.startswith("why"):
        if CAUSAL.search(ans):
            soft.append("definitive causal wording")
        if not HEDGE.search(ans):
            soft.append("no hedging words")
    if kind == "why_noevidence" and not re.search(r"(no clear|no evidence|nothing|no specific|not find|no filings|no news|unclear)", ans, re.I):
        soft.append("did not say that no evidence was found")
    if not kind.startswith("trap") and not re.search(r"(source|as of)", ans, re.I):
        soft.append("no source/timestamp line")
    return hard, soft


def get_key(record, suite, var="GEMINI_API_KEY"):
    """Return the API key or None, recording a precise reason (never the key itself)."""
    if var not in os.environ:
        record(suite, "api_key", "SKIP",
               f"env var {var} is ABSENT: the workflow is not passing the secret "
               "(old workflow file, or secret name/location wrong)")
        return None
    key = os.environ[var].strip()
    if not key:
        record(suite, "api_key", "SKIP",
               f"env var {var} is present but EMPTY: the secret name is not found or its value is blank")
        return None
    record(suite, "api_key", "INFO", f"{var} present (length {len(key)})")
    return key


# ---------------------------------------------------------------- suites
def run_llm(record, args):
    suite = "llm"
    key = get_key(record, suite)
    if not key:
        return
    model = pick_model(record, key, suite)
    if not model:
        return
    only = {x.strip() for x in os.environ.get("LLM_CASES", "").split(",") if x.strip()}
    todo = [c for c in CASES if not only or c[0] in only]
    done = []
    for case in todo:
        res = run_case(model, case, key)
        cid = case[0]
        if "error" in res:
            record(suite, cid, "ERROR", res["error"])
            if "429" in res["error"]:
                record(suite, "quota", "WARN", "rate/quota limit hit; stopping the suite early")
                break
            done.append(cid)
            continue
        done.append(cid)
        hard, soft = evaluate(case, res)
        status = "FAIL" if hard else ("REVIEW" if soft else "PASS")
        calls = "; ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in res["calls"]) or "none"
        record(suite, cid, status, f"calls: {calls} | issues: {hard + soft or 'none'}")
        print("    ANSWER >>> " + res["answer"].replace("\n", "\n              ") + "\n")
    left = [c[0] for c in todo if c[0] not in done]
    if left:
        record(suite, "not_run", "WARN", "cases not completed: " + ",".join(left)
               + "  -> rerun with the 'cases' input set to exactly this list")
    n = max(STATS["calls"], 1)
    record(suite, "usage", "INFO",
           f"model={model} calls={STATS['calls']} http429={STATS['http429']} other_errors={STATS['errors']} "
           f"in_tokens={STATS['in_tokens']} out_tokens={STATS['out_tokens']} avg_latency_s={STATS['latency'] / n:.1f}")
    print("\nRead every ANSWER above. REVIEW means a heuristic flagged it; judge by eye. "
          "Any invented number or cause is a failure regardless of status.")


GROUNDING_PROMPTS = [
    ("G1", "What is the latest news on KRN Heat Exchanger and Refrigeration (NSE: KRN)? Answer in 5 lines."),
    ("G2", "Why did Tempsens Instruments (NSE: TEMPSENS) share price rise on 3 November 2026? Answer in 5 lines, "
           "and say clearly if you cannot find a reliable reason."),
]


def run_grounding(record, args):
    suite = "grounding"
    key = get_key(record, suite)
    if not key:
        return
    model = pick_model(record, key, suite)
    if not model:
        return
    for gid, prompt in GROUNDING_PROMPTS:
        data, err = call(model, [{"role": "user", "parts": [{"text": prompt}]}], key,
                         tools=[{"google_search": {}}])
        if err:
            record(suite, gid, "ERROR", err)
            time.sleep(PAUSE)
            continue
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if "text" in p)
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        sites = [(c.get("web") or {}).get("title", "?") for c in chunks][:8]
        queries = gm.get("webSearchQueries") or []
        status = "REVIEW" if chunks else "FAIL"
        record(suite, gid, status,
               f"grounding_chunks={len(chunks)} queries={queries[:3]} sources={sites}")
        print("    ANSWER >>> " + text.strip().replace("\n", "\n              ") + "\n")
        time.sleep(PAUSE)
    print("\nJudge by eye: are the dates/claims in the answers supported by the listed sources? "
          "Grounded answers are 'news/context', never verified facts.")


# ================================================================ Groq (OpenAI-compatible) + fallback chain
GROQ_API = "https://api.groq.com/openai/v1"
GROQ_PREFERRED = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b",
                  "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3-32b"]


def _lower_types(o):
    if isinstance(o, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_lower_types(x) for x in o]
    return o


def groq_tools():
    return [{"type": "function", "function": {"name": d["name"], "description": d["description"],
                                              "parameters": _lower_types(d["parameters"])}} for d in DECLS]


def groq_call(model, messages, key, tools=None, tries=3):
    err = ""
    for attempt in range(tries):
        t0 = time.time()
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        r = requests.post(f"{GROQ_API}/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=body, timeout=120)
        STATS["calls"] += 1
        STATS["latency"] += time.time() - t0
        if r.status_code == 200:
            data = r.json()
            u = data.get("usage", {})
            STATS["in_tokens"] += u.get("prompt_tokens", 0)
            STATS["out_tokens"] += u.get("completion_tokens", 0)
            return data, None
        txt = _redact(r.text, key)
        err = f"HTTP {r.status_code}: {txt[:400]}"
        if r.status_code == 429:
            STATS["http429"] += 1
            if re.search(r"per day|TPD|RPD", txt, re.I):
                break  # daily limit: waiting will not help
            if attempt < tries - 1:
                try:
                    wait = min(int(float(r.headers.get("retry-after", "30"))) + 2, 70)
                except ValueError:
                    wait = 30
                time.sleep(wait)
                continue
        else:
            STATS["errors"] += 1
        break
    return None, err


def groq_pick_model(record, key, suite):
    r = requests.get(f"{GROQ_API}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code != 200:
        record(suite, "groq_list_models", "FAIL", f"HTTP {r.status_code}: {_redact(r.text, key)[:200]}")
        return None
    names = [m["id"] for m in r.json().get("data", [])]
    record(suite, "groq_list_models", "INFO", f"{len(names)} models; examples: {', '.join(sorted(names)[:14])}")
    forced = os.environ.get("GROQ_MODEL", "").strip()
    cands = ([forced] if forced else []) + [m for m in GROQ_PREFERRED if m in names]
    for m in dict.fromkeys(cands):
        data, err = groq_call(m, [{"role": "user", "content": "Reply with the single word OK."}], key, tries=2)
        if data:
            record(suite, f"groq_model:{m}", "PASS", "test call succeeded on this key")
            return m
        record(suite, f"groq_model:{m}", "FAIL", err)
        time.sleep(PAUSE)
    record(suite, "groq_model_selection", "FAIL", "no candidate Groq model worked")
    return None


def run_case_groq(model, case, key):
    cid, kind, turns, expect_tools, expect_sym = case
    messages = [{"role": "system", "content": SYSTEM}]
    allowed, final_calls, answer = "", [], ""
    tools = groq_tools()
    for turn in turns:
        messages.append({"role": "user", "content": turn})
        allowed += " " + turn
        final_calls = []
        for _ in range(5):
            data, err = groq_call(model, messages, key, tools=tools)
            if err:
                return {"error": err}
            msg = ((data.get("choices") or [{}])[0]).get("message") or {}
            tcs = msg.get("tool_calls") or []
            if not tcs:
                answer = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
                if not answer:
                    return {"error": "empty response"}
                messages.append({"role": "assistant", "content": answer})
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                final_calls.append((fn.get("name"), args))
                tool = TOOLS.get(fn.get("name"))
                result = tool(args) if tool else {"error": "unknown tool"}
                allowed += " " + json.dumps(result)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": json.dumps(result)})
            time.sleep(PAUSE)
        else:
            return {"error": "tool loop did not finish in 5 steps"}
        time.sleep(PAUSE)
    return {"answer": answer, "calls": final_calls, "allowed": allowed}


def _report(record, suite, case, res, label=""):
    hard, soft = evaluate(case, res)
    status = "FAIL" if hard else ("REVIEW" if soft else "PASS")
    calls = "; ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in res["calls"]) or "none"
    record(suite, case[0], status, f"{label}calls: {calls} | issues: {hard + soft or 'none'}")
    print("    ANSWER >>> " + res["answer"].replace("\n", "\n              ") + "\n")


def _cases_to_run():
    only = {x.strip() for x in os.environ.get("LLM_CASES", "").split(",") if x.strip()}
    return [c for c in CASES if not only or c[0] in only]


def run_groq(record, args):
    suite = "groq"
    key = get_key(record, suite, "GROQ_API_KEY")
    if not key:
        return
    model = groq_pick_model(record, key, suite)
    if not model:
        return
    todo, done = _cases_to_run(), []
    for case in todo:
        res = run_case_groq(model, case, key)
        if "error" in res:
            record(suite, case[0], "ERROR", res["error"])
            if "429" in res["error"]:
                record(suite, "quota", "WARN", "Groq limit hit; stopping early")
                break
            done.append(case[0])
            continue
        done.append(case[0])
        _report(record, suite, case, res)
    left = [c[0] for c in todo if c[0] not in done]
    if left:
        record(suite, "not_run", "WARN", "cases not completed: " + ",".join(left))
    n = max(STATS["calls"], 1)
    record(suite, "usage", "INFO", f"model={model} calls={STATS['calls']} http429={STATS['http429']} "
           f"other_errors={STATS['errors']} in_tokens={STATS['in_tokens']} out_tokens={STATS['out_tokens']} "
           f"avg_latency_s={STATS['latency'] / n:.1f}")
    print("\nRead every ANSWER above; compare with the Gemini answers for the same case ids.")


def run_chain(record, args):
    """Primary/fallback test: each case runs on the first provider that answers.
    If a provider errors (429/503/other) the case restarts from turn 1 on the next provider."""
    suite = "chain"
    gem_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    providers = {}
    if gem_key:
        gm = pick_model(record, gem_key, suite)
        if gm:
            providers["gemini:" + gm] = lambda case, m=gm: run_case(m, case, gem_key)
    if groq_key:
        qm = groq_pick_model(record, groq_key, suite)
        if qm:
            providers["groq:" + qm] = lambda case, m=qm: run_case_groq(m, case, groq_key)
    order = [x.strip() for x in os.environ.get("CHAIN_ORDER", "gemini,groq").split(",") if x.strip()]
    chain = [(n, f) for o in order for n, f in providers.items() if n.startswith(o + ":")]
    if not chain:
        record(suite, "providers", "FAIL", "no usable provider (check keys, quotas, model availability)")
        return
    record(suite, "providers", "INFO", "chain order: " + " -> ".join(n for n, _ in chain))
    used = {}
    for case in _cases_to_run():
        res, tried = None, []
        for name, fn in chain:
            r = fn(case)
            if "error" in r:
                tried.append(f"{name} failed ({r['error'][:90]})")
                continue
            res, who = r, name
            break
        if res is None:
            record(suite, case[0], "FAIL", "all providers failed: " + " | ".join(tried))
            continue
        used[who] = used.get(who, 0) + 1
        _report(record, suite, case, res, label=f"[{who}{' after fallback' if tried else ''}] ")
    record(suite, "provider_usage", "INFO", f"cases answered per provider: {used}; "
           f"calls={STATS['calls']} http429={STATS['http429']} other_errors={STATS['errors']}")
