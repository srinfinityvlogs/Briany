# SPDX-License-Identifier: Apache-2.0
"""Question router: free text -> one tool call. The model only ROUTES; it never answers.

Tools map to evidence-assembler packets. Every route is validated in code before use; if no LLM
provider works, a rule-based router (keywords, dates, periods, company names from the resolver) takes over.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date, timedelta

from briany.llm import LlmError
from briany.symbols import find_index

PERIODS = ("1w", "1m", "3m", "6m", "1y", "ytd")
UNSUPPORTED_REASONS = ("comparison", "compare_period", "advice", "prediction", "market_data", "index_not_covered",
                       "period", "company_not_found", "other")
MAX_QUESTION = 300

TOOLS = [
    {"name": "why_moved",
     "description": "Explain a stock's price move on a day: the move, market context and dated filings, corporate "
                    "actions and news. Use for 'why is X up/down', 'what happened to X', 'X jumped/fell'.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "company name or NSE symbol exactly as the user wrote it"},
         "date": {"type": "string", "description": "YYYY-MM-DD of the move if the user named a day; omit for the latest"}},
         "required": ["query"]}},
    {"name": "period_return",
     "description": "Price return of a stock, or change in level of an NSE index, over a period (1 week, 1 month, "
                    "3 months, 6 months, 1 year, year to date).",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "company name or NSE symbol exactly as the user wrote it"},
         "period": {"type": "string", "enum": list(PERIODS), "description": "1w, 1m, 3m, 6m, 1y or ytd"}},
         "required": ["query", "period"]}},
    {"name": "compare_returns",
     "description": "Compare TWO stocks, ETFs or NSE indices over the same period (1w, 1m, 3m, 6m, 1y, ytd). Use for "
                    "'compare TCS and Infosys over 1 year', 'tcs vs infy ytd', 'nifty bank vs nifty it 6 months'. Needs both "
                    "names and a period.",
     "parameters": {"type": "object", "properties": {
         "query_a": {"type": "string", "description": "first company or index exactly as the user wrote it"},
         "query_b": {"type": "string", "description": "second company or index exactly as the user wrote it"},
         "period": {"type": "string", "enum": list(PERIODS), "description": "1w, 1m, 3m, 6m, 1y or ytd"}},
         "required": ["query_a", "query_b", "period"]}},
    {"name": "events",
     "description": "Recent exchange filings, board meetings, upcoming ex-dates and the next results date for a stock. "
                    "Use for 'when are results', 'any announcements', 'latest filings', 'dividend date'.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "company name or NSE symbol exactly as the user wrote it"}},
         "required": ["query"]}},
    {"name": "index_close",
     "description": "Where an NSE index (Nifty 50, Nifty Bank, Nifty IT, India VIX, ...) closed on a day: close, change, "
                    "open/high/low. Use for 'how is the nifty', 'bank nifty today', 'why is the nifty down'. Not for BSE "
                    "indices such as the Sensex and not for index returns over a period.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "index name exactly as the user wrote it"},
         "date": {"type": "string", "description": "YYYY-MM-DD if the user named a day; omit for the latest"}},
         "required": ["query"]}},
    {"name": "unsupported",
     "description": "Use when the question is NOT about one listed stock's move, return or filings, or an NSE index's "
                    "close: comparing more than two things, comparing without a period (compare_period), buy/sell advice, "
                    "price predictions, market-wide data (FII/DII, bulk deals), BSE indices such as the Sensex "
                    "(index_not_covered), or anything else.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string", "enum": list(UNSUPPORTED_REASONS), "description": "why it is unsupported"}},
         "required": ["reason"]}},
    {"name": "smalltalk",
     "description": "Greetings, thanks, or asking what the bot can do.", "parameters": {"type": "object", "properties": {}}},
]

SYSTEM = """You route questions about Indian (NSE) stocks to exactly one tool. You never answer the question yourself.
Rules:
1. Call exactly one tool.
2. query: copy the company name or symbol as the user wrote it. Do not guess or expand it.
3. date: only if the user named a day. Today is {today}. Resolve 'yesterday' and dates without a year against it.
4. period_return needs a period from the allowed list; anything else (for example '2 years') is unsupported.
5. Comparing two stocks or indices over a period uses compare_returns. Comparing more than two (comparison), comparing
   with no period (compare_period), buy/sell advice, price predictions and market-wide data are unsupported.
   An NSE index's close on a day uses index_close; the Sensex or another BSE index is unsupported
   (index_not_covered). An index's return over a period uses period_return with the index name.
6. If the question names no company ('why did it fall?', 'and last month?', 'what about it?') and a previous
   company is given below, use that previous symbol as the query. Only say company_not_found when none is given.
{context}"""


@dataclass(frozen=True)
class Route:
    tool: str
    args: dict
    source: str = ""


@dataclass(frozen=True)
class Context:
    last_symbol: str = ""
    last_name: str = ""


class RouteError(ValueError):
    pass


# ------------------------------------------------------------------ validation
def validate_route(name, args, today: str, source: str = "") -> Route:
    """Check a tool call from the model. Returns a cleaned Route or raises RouteError."""
    args = args if isinstance(args, dict) else {}
    if name in ("why_moved", "period_return", "events", "index_close"):
        query = str(args.get("query") or "").strip()
        tail = re.search(r"\(([A-Za-z0-9&.-]{2,20})\)\s*$", query)          # "Tata Steel Limited (TATASTEEL)"
        if tail:
            query = tail.group(1)
        if not query or len(query) > 80:
            raise RouteError("query must be 1-80 characters")
        if name == "events":
            return Route("events", {"query": query}, source)
        if name == "period_return":
            period = str(args.get("period") or "").strip().lower()
            if period not in PERIODS:
                raise RouteError(f"unknown period {period!r}")
            return Route("period_return", {"query": query, "period": period}, source)
        raw = str(args.get("date") or "").strip()
        out = {"query": query}
        tool = "index_close" if name == "index_close" else "why_moved"
        if raw:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                raise RouteError("date must be YYYY-MM-DD")
            try:
                d = Date.fromisoformat(raw)
            except ValueError:
                raise RouteError("invalid date") from None
            if d > Date.fromisoformat(today):
                raise RouteError("date is in the future")
            if raw != today:                 # "today" means the latest close; keeps the stale-data check active
                out["date"] = raw
        return Route(tool, out, source)
    if name == "compare_returns":
        qa, qb = (str(args.get(k) or "").strip() for k in ("query_a", "query_b"))
        for q in (qa, qb):
            if not q or len(q) > 80:
                raise RouteError("each query must be 1-80 characters")
        if qa.lower() == qb.lower():
            raise RouteError("the two names are the same")
        period = str(args.get("period") or "").strip().lower()
        if period not in PERIODS:
            raise RouteError(f"unknown period {period!r}")
        return Route("compare_returns", {"queries": [qa, qb], "period": period}, source)
    if name == "unsupported":
        reason = str(args.get("reason") or "other").strip().lower()
        return Route("unsupported", {"reason": reason if reason in UNSUPPORTED_REASONS else "other"}, source)
    if name == "smalltalk":
        return Route("smalltalk", {}, source)
    raise RouteError(f"unknown tool {name!r}")


# ------------------------------------------------------------------ rule-based fallback
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
STOP = frozenset("""a an the is are was were be been why what whats how when who which did does do has have had it its
this that these those of for on in at to from by with and or but not up down rise rises rising risen rose fall falls
falling fell jump jumped jumps surge surged spike spiked crash crashed drop dropped drops rally rallied plunge
plunged slump slumped tank tanked today yesterday now latest recent current stock stocks share shares price prices
return returns gain gained performance month months week weeks year years ytd since last past over about tell me
show give please much many percent pct move moved moving going gone happen happened happening news result results
announce announcement announcements filing filings dividend due upcoming next doing trading looking anything any some there""".split())
SMALL = re.compile(r"^\s*/?(hi|hello|hey|help|start|thanks|thank you|namaste)\b|"
                   r"^\s*(what can you do|what do you do|who are you|how (do|does) (this|it|you) work|can you help)", re.I)
ADVICE = re.compile(r"\b(should i|shall i|buy|sell|hold|invest|investment|target price|good stock|worth buying|safe to)\b", re.I)
PREDICT = re.compile(r"\b(will|would it|going to|predict|prediction|forecast|expect|expected to|tomorrow|next week|next month)\b", re.I)
MARKET = re.compile(r"\b(fiis?|diis?|fpis?|bulk deals?|block deals?|index|indices|market wide)\b", re.I)
BSE_INDEX = re.compile(r"\b(sensex|bankex|bse\s*(sensex|\d+|smallcap|midcap|largecap|allcap))\b", re.I)
COMPARE = re.compile(r"\b(compare|versus|vs\.?|better than|against)\b", re.I)
RETURN = re.compile(r"\b(returns?|gains?|gained|performance|performed|how much has|since|grown|growth)\b", re.I)
EVENTS = re.compile(r"\b(results?|earnings|announce\w*|filings?|board meetings?|dividends?|bonus|split|due|upcoming|"
                    r"latest news|corporate actions?)\b", re.I)
WHY = re.compile(r"\b(why|what happened|what is happening|what's happening|how come|reason)\b|\b(up|down|rise|rising|"
                 r"rose|fall|falling|fell|jump\w*|surg\w*|spik\w*|crash\w*|drop\w*|rall\w*|plung\w*|slump\w*|tank\w*)\b", re.I)
PRONOUN = re.compile(r"\b(it|its|that|this one|that stock|the same|same stock|them)\b|^\s*(and|what about|how about)\b", re.I)


def parse_period(q: str):
    """-> (period, found). found is True when the question names a period, even an unsupported one."""
    t = q.lower()
    if re.search(r"\b(ytd|year to date|this year|so far this year)\b", t):
        return "ytd", True
    m = re.search(r"\b(\d+|one|a)\s*[- ]?(w|wk|week|weeks|m|mo|month|months|y|yr|year|years)\b", t)
    if m:
        n = {"one": 1, "a": 1}.get(m.group(1)) or int(m.group(1))
        unit = m.group(2)
        if unit.startswith("w"):
            return ("1w", True) if n == 1 else (None, True)
        if unit.startswith("m"):
            return ({1: "1m", 3: "3m", 6: "6m", 12: "1y"}.get(n), True)
        return ("1y", True) if n == 1 else (None, True)
    for words, period in ((r"\b(last|past) week\b|\bweekly\b", "1w"), (r"\b(last|past) month\b|\bmonthly\b", "1m"),
                          (r"\b(last|past) year\b|\bannual\b", "1y")):
        if re.search(words, t):
            return period, True
    return None, False


def parse_date(q: str, today: Date):
    t = q.lower()
    try:
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
        if m:
            return Date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", t)
        if m:
            return Date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON}\b(?:,?\s+(\d{{4}}))?", t)
        day, mon, year = (int(m.group(1)), MONTHS[m.group(2)], m.group(3)) if m else (None, None, None)
        if not m:
            m = re.search(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", t)
            if m:
                day, mon, year = int(m.group(2)), MONTHS[m.group(1)], m.group(3)
        if m:
            d = Date(int(year) if year else today.year, mon, day)
            if not year and d > today:
                d = Date(today.year - 1, mon, day)
            return d.isoformat()
    except ValueError:
        return None
    if re.search(r"\byesterday\b", t):
        return (today - timedelta(days=1)).isoformat()
    return None


def _noise(word: str) -> bool:
    return word.lower() in STOP or re.fullmatch(r"[\d.,%]+", word) is not None


def find_companies(q: str, resolver):
    """Non-overlapping company mentions, longest first: [(text_as_typed, symbol)]."""
    words = re.findall(r"[A-Za-z0-9&.'-]+", q)
    taken, found = [False] * len(words), []
    for size in (4, 3, 2, 1):
        for i in range(len(words) - size + 1):
            if any(taken[i:i + size]):
                continue
            window = words[i:i + size]
            if _noise(window[0]) or _noise(window[-1]):       # a name never starts or ends with filler or a number
                continue
            text = " ".join(window)
            if size == 1 and len(text) < 3:
                continue
            res = resolver.resolve(text)
            ok = res.status == "exact" or (res.status == "confident" and (size > 1 or len(text) >= 5))
            if ok and res.best.kind == "equity":
                found.append((i, text, res.best.symbol))
                for j in range(i, i + size):
                    taken[j] = True
    found.sort()
    return [(text, sym) for _i, text, sym in found]


def find_indices(q: str):
    """Non-overlapping NSE index mentions, longest first: [(text_as_typed, Instrument)] in the order they appear."""
    words = re.findall(r"[A-Za-z0-9&.'-]+", q)
    taken, found = [False] * len(words), []
    for size in (7, 6, 5, 4, 3, 2, 1):
        for i in range(len(words) - size + 1):
            if any(taken[i:i + size]):
                continue
            text = " ".join(words[i:i + size])
            inst = find_index(text)
            if inst:
                found.append((i, text, inst))
                for j in range(i, i + size):
                    taken[j] = True
    found.sort(key=lambda x: x[0])
    return [(text, inst) for _i, text, inst in found]


def _without(q: str, texts) -> str:
    for t in texts:
        q = re.sub(re.escape(t), " ", q, count=1, flags=re.I)
    return q


def rule_route(question: str, resolver, today: str, context: Context | None = None) -> Route:
    q = " ".join(question.split())[:MAX_QUESTION]
    t = Date.fromisoformat(today)
    context = context or Context()
    if SMALL.search(q):
        return Route("smalltalk", {}, "rules")
    indices = find_indices(q)
    companies = find_companies(_without(q, [t for t, _i in indices]), resolver)    # an index name is not a company
    symbols = {s for _t, s in companies}
    if ADVICE.search(q):
        return Route("unsupported", {"reason": "advice"}, "rules")
    if PREDICT.search(q) and not re.search(r"\bwhy\b", q, re.I):
        return Route("unsupported", {"reason": "prediction"}, "rules")
    mentions = sorted([(q.lower().find(t.lower()), t, s) for t, s in companies] +
                      [(q.lower().find(t.lower()), t, i.symbol) for t, i in indices])
    distinct = {s for _p, _t, s in mentions}
    if len(distinct) == 2:
        period, found = parse_period(q)
        first, second = [t for _p, t, _s in mentions][:2]
        if found and not period:
            return Route("unsupported", {"reason": "period"}, "rules")
        if period:
            return Route("compare_returns", {"queries": [first, second], "period": period}, "rules")
        return Route("unsupported", {"reason": "compare_period" if COMPARE.search(q) or RETURN.search(q) else "comparison"},
                     "rules")
    if len(distinct) > 2 or COMPARE.search(q):
        return Route("unsupported", {"reason": "comparison"}, "rules")
    if BSE_INDEX.search(q) and not symbols:
        return Route("unsupported", {"reason": "index_not_covered"}, "rules")
    last_is_index = bool(context.last_symbol and find_index(context.last_symbol))
    if not symbols and (indices or (last_is_index and not MARKET.search(q) and
                                    (PRONOUN.search(q) or parse_period(q)[1] or parse_date(q, t)))):
        period, found = parse_period(q)
        if found or RETURN.search(q):
            if not period:
                return Route("unsupported", {"reason": "period"}, "rules")
            return Route("period_return", {"query": indices[0][0] if indices else context.last_symbol, "period": period},
                         "rules")
        args = {"query": indices[0][0] if indices else context.last_symbol}
        d = parse_date(q, t)
        if d and Date.fromisoformat(d) <= t:
            args["date"] = d
        return Route("index_close", args, "rules")
    if MARKET.search(q) and not symbols:
        return Route("unsupported", {"reason": "market_data"}, "rules")
    if companies:
        query = companies[0][0]
    elif context.last_symbol and (PRONOUN.search(q) or parse_period(q)[1] or parse_date(q, t)):
        query = context.last_symbol
    else:
        return Route("unsupported", {"reason": "company_not_found"}, "rules")
    period, found = parse_period(q)
    if found or RETURN.search(q):
        if not period:
            return Route("unsupported", {"reason": "period"}, "rules")
        return Route("period_return", {"query": query, "period": period}, "rules")
    if EVENTS.search(q) and not re.search(r"\bwhy\b", q, re.I):
        return Route("events", {"query": query}, "rules")
    args = {"query": query}
    d = parse_date(q, t)
    if d and Date.fromisoformat(d) <= t:
        args["date"] = d
    return Route("why_moved", args, "rules")


# ------------------------------------------------------------------ entry point
@dataclass(frozen=True)
class Routed:
    route: Route
    attempts: tuple = ()          # ((provider, outcome), ...)
    fell_back: bool = False


def _fix_index_route(route: Route, today: str) -> Route:
    """A model that sends an index name to a stock tool is corrected here: an index has no filings, and its return over a
    period is not offered."""
    if route.tool in ("why_moved", "events") and find_index(route.args.get("query", "")):
        if route.tool == "why_moved":
            return Route("index_close", route.args, route.source)
        return Route("unsupported", {"reason": "other"}, route.source)
    return route


def route_question(question, chain, resolver, today: str, context: Context | None = None) -> Routed:
    q = " ".join(str(question or "").split())[:MAX_QUESTION]
    if not q:
        return Routed(Route("smalltalk", {}, "rules"))
    context = context or Context()
    ctx_line = (f"Previous company in this conversation: {context.last_symbol} "
                "(for a follow-up, use exactly this symbol as the query)." if context.last_symbol
                else "No previous company in this conversation.")
    system = SYSTEM.format(today=today, context=ctx_line)
    holder = {}

    def validate(call):
        holder["route"] = validate_route(call.name, call.args, today)

    try:
        _call, provider, attempts = chain.route(system, q, TOOLS, validate)
        r = holder["route"]
        if r.tool == "unsupported" and r.args.get("reason") in ("company_not_found", "other"):
            second = rule_route(q, resolver, today, context)          # the rules only answer when a company resolves
            if second.tool in ("why_moved", "period_return", "events", "index_close") or (
                    second.tool == "unsupported" and second.args.get("reason") == "index_not_covered"):
                return Routed(Route(second.tool, second.args, "rules:override"), tuple(attempts), False)
        if r.tool == "unsupported" and r.args.get("reason") in ("market_data", "comparison"):   # sees indices as market data
            second = rule_route(q, resolver, today, context)
            if second.tool in ("index_close", "period_return", "compare_returns") or (
                    second.tool == "unsupported" and second.args.get("reason") in ("index_not_covered", "compare_period")):
                return Routed(Route(second.tool, second.args, "rules:override"), tuple(attempts), False)
        fixed = _fix_index_route(Route(r.tool, r.args, f"llm:{provider}"), today)
        return Routed(fixed, tuple(attempts), False)
    except LlmError as e:
        attempts = tuple(tuple(a.split("=", 1)) for a in str(e).split(": ", 1)[-1].split(", ")) if e.kind == "all_failed" else ()
        return Routed(rule_route(q, resolver, today, context), attempts, True)
