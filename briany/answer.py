# SPDX-License-Identifier: Apache-2.0
"""From a verified packet to a message: deterministic text, model-written text, validator, footer.

The model may only REPHRASE a packet that code built. validate_text() rejects any model text that contains a
number, date, clock time or ticker not found in the packet, causal wording, advice or predictions, a wrong
direction, a missing key figure, a missing required caveat, or more than MAX_LINES lines. Rejected text is
replaced by fallback_text(), which is built from the packet alone and passes the same validator.
English only.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from briany.evidence import IST, INDEX_SOURCE, MONTHS, PRICE_SOURCE, Packet, human_date

MAX_LINES = 10
MAX_CHARS = 1400
MAX_FACTORS_SHOWN = 4
KIND_PRIORITY = ("corporate_action", "announcement", "news", "board_meeting")

# a required caveat must appear (case-insensitive) in any text derived from a packet carrying that gap.
# Phrases are substrings of the gap texts written by the assembler, so the fallback satisfies them as well.
REQUIRED_PHRASES = {
    "no_clear_evidence": "no clear evidence found",
    "corporate_action_on_date": "corporate action",
    "unverified_large_move": "unlisted split",
    "news_not_covered": "not checked",
    "news_unavailable": "could not be checked",
    "announcements_unavailable": "could not be checked",
    "board_meetings_unavailable": "could not be checked",
    "calendar_unavailable": "could not be checked",
    "flows_unavailable": "could not be fetched",
    "data_may_be_stale": "newer data may exist",
    "price_return_only": "excludes dividends",
    "next_results_not_announced": "no upcoming results meeting",
    "actions_unavailable": "could not be verified",
    "index_file_unavailable": "could not be fetched",
    "index_change_missing": "no change figure",
}

CAUSAL = re.compile(
    r"\b(because|due to|owing to|caused by|driven by|thanks to|as a result of|on account of|attributed to|"
    r"the reason (is|was)|led to|triggered by|fuell?ed by|on the back of|result of)\b", re.I)
ADVICE = re.compile(
    r"\b(you should|should (buy|sell|hold|invest)|i recommend|recommend(ed)? (buying|selling|holding)|buy now|"
    r"sell now|good time to|worth buying|bullish|bearish|price target|target price|"
    r"(will|going to|expected to|likely to|set to) (rise|fall|go up|go down|increase|decrease|rally|crash|jump|drop))\b", re.I)
UP_WORDS = re.compile(r"\b(up|rose|risen|rise|rises|gain|gained|gains|higher|jumped|surged|climbed|advanced|rallied)\b", re.I)
DOWN_WORDS = re.compile(r"\b(down|fell|fallen|fall|falls|drop|dropped|declined|decline|lower|slumped|plunged|"
                        r"tumbled|crashed|slipped)\b", re.I)
POSSIBLE = re.compile(r"\b(possible|possibly|may have|might have|could have|potential|potentially)\b", re.I)
WHITELIST_CAPS = frozenset("NSE BSE FII DII FPI ETF IPO AGM EGM SEBI RBI YTD INR LTD NAV EPS PE FY".split())

_MON_RE = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON_RE}(?:,?\s+(\d{{4}}))?\b", re.I)
DATE_MDY = re.compile(rf"\b{_MON_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)
CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s?(?:am|pm)\b", re.I)
NUMBER = re.compile(r"(?<![\w.])[+\-−]?₹?\s?\d[\d,]*(?:\.\d+)?%?")
URL = re.compile(r"https?://\S+")
BULLET = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+", re.M)
ALLCAPS = re.compile(r"\b[A-Z][A-Z0-9&-]{1,11}\b")


SUMMARY_BANNED = re.compile(r"\b(factors?|filings?|announcements?|news|headlines?|board meetings?)\b", re.I)
EXPLAIN = re.compile(r"\b(why|what happened|what is happening|what's happening|how come|reason|what caused)\b", re.I)
ASKS_UP = re.compile(r"\b(up|rise|rising|rose|jump\w*|surg\w*|rall\w*|gain\w*|higher)\b", re.I)
ASKS_DOWN = re.compile(r"\b(down|fall|falling|fell|drop\w*|crash\w*|plung\w*|slump\w*|tank\w*|lower)\b", re.I)
MAX_LINK_CHARS = 200              # longer links (Google News) are not printed in plain text

# ------------------------------------------------------------------ packet helpers
def _dec(x) -> Decimal | None:
    try:
        return abs(Decimal(str(x).replace("₹", "").replace(",", "").replace("%", "").replace("−", "-").replace("+", "").strip()))
    except InvalidOperation:
        return None


def packet_blob(p: Packet) -> str:
    parts = [p.name, p.symbol]
    for f in p.facts + p.context:
        parts += [f.label, f.value, f.date or ""]
    for f in p.factors:
        parts += [f.kind, f.when, f.timing, f.title, f.detail, f.source]
    parts += [g.text for g in p.gaps]
    return "\n".join(x for x in parts if x)


def _dates_in(text: str):
    """{(year|None, month, day)} for every date mentioned in text."""
    out = set()
    for m in DATE_ISO.finditer(text):
        out.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in DATE_DMY.finditer(text):          # groups: 1 day, 2 month, 3 year
        out.add((int(m.group(3)) if m.group(3) else None, MONTHS.index(m.group(2).title()) + 1, int(m.group(1))))
    for m in DATE_MDY.finditer(text):          # groups: 1 month, 2 day, 3 year
        out.add((int(m.group(3)) if m.group(3) else None, MONTHS.index(m.group(1).title()) + 1, int(m.group(2))))
    return out


def _strip_dates(text: str) -> str:
    for rx in (DATE_ISO, DATE_DMY, DATE_MDY):
        text = rx.sub(" ", text)
    return text


def _allowed_numbers(p: Packet, blob: str):
    allowed = set()
    clean = _strip_dates(URL.sub(" ", blob))
    for tok in NUMBER.findall(clean):
        d = _dec(tok)
        if d is not None:
            allowed.add(d)
    for f in p.facts + p.context:
        if f.raw is not None:
            for r in (f.raw, round(f.raw, 2)):
                allowed.add(abs(Decimal(str(r))))
    allowed.update(Decimal(n) for n in range(0, len(p.factors) + 2))
    return allowed


def main_change(p: Packet):
    return next((f for f in p.facts if f.label.startswith("Change from previous close")), None)


def required_phrases(p: Packet) -> list:
    return [REQUIRED_PHRASES[g.code] for g in p.gaps if g.code in REQUIRED_PHRASES]


# ------------------------------------------------------------------ validator
def shown_candidates(p: Packet):
    """Factors the message may list. A corporate action already stated as a verified fact (the ex-date caveat)
    is not repeated as a hedged 'possible factor'."""
    if any(g.code == "corporate_action_on_date" for g in p.gaps):
        return [f for f in p.factors if f.kind != "corporate_action"]
    return list(p.factors)


def _safety_problems(text: str, p: Packet):
    """Wrong content: invented numbers, dates, clock times, tickers; causal wording; advice; wrong direction.
    Returns (problems, set_of_numbers_seen)."""
    problems = []
    blob = packet_blob(p)
    if CAUSAL.search(text):
        problems.append(f"causal wording: {CAUSAL.search(text).group(0)!r}")
    if ADVICE.search(text):
        problems.append(f"advice or prediction: {ADVICE.search(text).group(0)!r}")

    packet_dates = _dates_in(blob) | {tuple(int(x) for x in f.date.split("-")) for f in p.facts + p.context if f.date}
    packet_dates |= {tuple(int(x) for x in f.when[:10].split("-")) for f in p.factors if f.when}
    for y, m, d in _dates_in(text):
        if not any(pm == m and pd == d and (y is None or py is None or py == y) for py, pm, pd in packet_dates):
            problems.append(f"date not in the data: {d} {MONTHS[m - 1]}{' ' + str(y) if y else ''}")
    for m in CLOCK.finditer(text):                                        # a ratio in an index name ('50:25:25') is data
        if m.group(0).lower() not in blob.lower():
            problems.append(f"clock time {m.group(0)!r}: times are added by code, not the text")
            break

    body = BULLET.sub("", _strip_dates(URL.sub(" ", text)))
    allowed, seen = _allowed_numbers(p, blob), set()
    for tok in NUMBER.findall(body):
        d = _dec(tok)
        if d is not None:
            seen.add(d)
            if d not in allowed:
                problems.append(f"number not in the data: {tok.strip()}")

    chg = main_change(p)
    if chg and chg.raw is not None and chg.raw != 0:                      # direction must agree with the sign
        target = abs(Decimal(str(chg.raw)))
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            if any(_dec(t) == target for t in NUMBER.findall(sentence)):
                if chg.raw > 0 and DOWN_WORDS.search(sentence) and not UP_WORDS.search(sentence):
                    problems.append("direction contradicts the data (the price rose)")
                if chg.raw < 0 and UP_WORDS.search(sentence) and not DOWN_WORDS.search(sentence):
                    problems.append("direction contradicts the data (the price fell)")

    blob_low = blob.lower()                                               # tickers and names must come from the packet
    for tok in {t.rstrip("-") for t in ALLCAPS.findall(text)}:                # 'G-Sec' is read as 'G-': the hyphen is not the name
        if len(tok) < 2:
            continue
        if tok not in WHITELIST_CAPS and not re.search(rf"(?<![a-z0-9]){re.escape(tok.lower())}(?![a-z0-9])", blob_low):
            problems.append(f"name or ticker not in the data: {tok}")
    return problems, seen


def validate_text(text: str, p: Packet) -> list:
    """Problems in a WHOLE message against the packet (empty list = acceptable)."""
    text = (text or "").strip()
    if not text:
        return ["empty text"]
    problems = []
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) > MAX_LINES or len(text) > MAX_CHARS:
        problems.append(f"too long ({len(lines)} lines, {len(text)} characters)")
    safety, seen = _safety_problems(text, p)
    problems += safety
    if p.kind == "move" and shown_candidates(p) and not POSSIBLE.search(text):
        problems.append("factors must be described as possible contributing factors")
    chg = main_change(p)
    if p.kind == "move" and p.status == "ok":
        close = p.facts[0] if p.facts else None
        for need in ([close] if close else []) + ([chg] if chg else []):
            if need.raw is not None and abs(Decimal(str(need.raw))) not in seen and _dec(need.value) not in seen:
                problems.append(f"missing key figure: {need.label}")
    if p.kind == "index" and p.status == "ok":
        for need in ([p.facts[0]] if p.facts else []) + ([chg] if chg else []):
            if need.raw is not None and abs(Decimal(str(need.raw))) not in seen and _dec(need.value) not in seen:
                problems.append(f"missing key figure: {need.label}")
    if p.kind in ("index_return", "compare") and p.status == "ok":
        for need in p.facts:
            if need.raw is not None and abs(Decimal(str(need.raw))) not in seen and _dec(need.value) not in seen:
                problems.append(f"missing key figure: {need.label}")
    if p.kind == "return" and p.status == "ok":
        ret = next((f for f in p.facts if f.label == "Price return"), None)
        if ret and _dec(ret.value) not in seen:
            problems.append("missing key figure: Price return")
    low = text.lower()
    for code, phrase in ((g.code, REQUIRED_PHRASES[g.code]) for g in p.gaps if g.code in REQUIRED_PHRASES):
        if phrase not in low:
            problems.append(f"required caveat missing ({code}): include {phrase!r}")
    return problems


def validate_summary(text: str, p: Packet) -> list:
    """Problems in the model-written OPENING SUMMARY of a move. It must state the close, its date and the change,
    and must not discuss factors, filings or news (code adds those)."""
    text = (text or "").strip()
    if not text:
        return ["empty text"]
    if p.kind != "move" or p.status != "ok" or not p.facts:
        return ["not a move packet"]
    problems = []
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) > 3 or len(text) > 500:
        problems.append(f"summary too long ({len(lines)} lines, {len(text)} characters)")
    if SUMMARY_BANNED.search(text):
        problems.append("the summary must not discuss factors, filings or news (code adds them)")
    safety, seen = _safety_problems(text, p)
    problems += safety
    close, chg = p.facts[0], main_change(p)
    if abs(Decimal(str(close.raw))) not in seen and _dec(close.value) not in seen:
        problems.append("missing key figure: the official close")
    cy, cm, cd = (int(x) for x in close.date.split("-"))
    if not any(m == cm and d == cd and (y is None or y == cy) for y, m, d in _dates_in(text)):
        problems.append("missing key figure: the date of the close")
    if chg and chg.raw is not None and abs(Decimal(str(chg.raw))) not in seen and _dec(chg.value) not in seen:
        problems.append("missing key figure: the change from the previous close")
    if chg is None:                      # a corporate action is involved: the raw price change is not a real move
        targets = {abs(Decimal(str(f.raw))) for f in p.facts[:2] if f.raw is not None}
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            if {_dec(t) for t in NUMBER.findall(sentence)} & targets and (UP_WORDS.search(sentence) or DOWN_WORDS.search(sentence)):
                problems.append("no direction words about the price when the change is not shown (corporate action)")
                break
    return problems


def premise_note(question: str, p: Packet) -> list:
    """One code-written line when the question assumes a direction the data does not show."""
    chg = main_change(p)
    if p.kind not in ("move", "index") or p.status != "ok" or chg is None or chg.raw is None:
        return []
    up, down = bool(ASKS_UP.search(question or "")), bool(ASKS_DOWN.search(question or ""))
    notes = []
    if up != down:
        if up and chg.raw <= 0:
            notes.append(f"Note: the change from the previous close was {chg.value}, which is not an increase.")
        if down and chg.raw >= 0:
            notes.append(f"Note: the change from the previous close was {chg.value}, which is not a decrease.")
    if p.kind == "index" and EXPLAIN.search(question or ""):
        notes.append("I can show where an index closed, but I don't explain index moves.")
    return notes


# ------------------------------------------------------------------ deterministic text
KIND_LABEL = {"announcement": "NSE filing", "corporate_action": "Corporate action", "board_meeting": "Board meeting"}


def _clip(text: str, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def select_factors(factors, limit=MAX_FACTORS_SHOWN):
    """A mix, not the first N: the latest item of each type (corporate action, filing, news, board meeting),
    then the most recent remaining items; returned in time order."""
    chosen = []
    for kind in KIND_PRIORITY:
        same = [f for f in factors if f.kind == kind]
        if same and len(chosen) < limit:
            chosen.append(max(same, key=lambda f: f.when))
    for f in sorted((f for f in factors if f not in chosen), key=lambda f: f.when, reverse=True):
        if len(chosen) >= limit:
            break
        chosen.append(f)
    return sorted(chosen, key=lambda f: f.when)


def _factor_line(f) -> str:
    label = KIND_LABEL.get(f.kind) or ("News (" + f.source.split(": ", 1)[-1] + ")" if f.kind == "news" else f.kind)
    title = _clip(f.title)
    detail = " ".join((f.detail or "").split())
    if f.kind == "announcement" and detail and title.lower() not in detail.lower():
        title = f"{title} - {_clip(detail, 80)}"                  # NSE categories are often just 'Updates'
    return f"- {label}, {f.timing}: {title}"


def _gap_lines(p: Packet, skip=()):
    out = []
    for g in p.gaps:
        if g.code in skip:
            continue
        out.append(f"No clear evidence found. {g.text}" if g.code == "no_clear_evidence" else g.text)
    return out


def fallback_text(p: Packet, notes=(), shown_out=None) -> str:
    """Built from the packet alone. Passes validate_text() by construction (tested for every packet type)."""
    if p.status == "clarify":
        return p.gaps[0].text if p.gaps else "Which company do you mean? Please give the company name or NSE symbol."
    who = p.name if p.kind in ("index", "index_return", "compare") else (f"{p.name} ({p.symbol})" if p.symbol else "this company")
    if p.status == "refused":
        head = f"I can't compare {who} for this period." if p.kind == "compare" else f"I can't give a reliable figure for {who}."
        lines = [head] + _gap_lines(p)
        lines += [f"- {f.label}: {f.value}" + (f" ({human_date(f.date)})" if f.date else "") for f in p.facts[:3]]
        return "\n".join(lines[:MAX_LINES])
    if p.kind == "move":
        return "\n".join(_move_lines(p, who, None, notes, shown_out)[:MAX_LINES])
    if p.kind == "return":
        return "\n".join(_return_lines(p, who)[:MAX_LINES])
    if p.kind == "index":
        return "\n".join(_index_lines(p, who, notes)[:MAX_LINES])
    if p.kind == "index_return":
        return "\n".join(_index_return_lines(p, who)[:MAX_LINES])
    if p.kind == "compare":
        return "\n".join(_compare_lines(p)[:MAX_LINES])
    return "\n".join(_events_lines(p, who, shown_out)[:MAX_LINES])


def compose_move(p: Packet, summary_text: str, notes=(), shown_out=None) -> str:
    """Model-written opening summary + code-written factors and caveats."""
    who = f"{p.name} ({p.symbol})" if p.symbol else "this company"
    summary = [l.strip() for l in summary_text.strip().splitlines() if l.strip()]
    return "\n".join(_move_lines(p, who, summary, notes, shown_out)[:MAX_LINES])


def _move_head(p: Packet, who: str):
    close, prev, chg = p.facts[0], p.facts[1], main_change(p)
    head = [f"{who} closed at {close.value} on {human_date(close.date)}."]
    head.append(f"That is {chg.value} from the previous close of {prev.value}." if chg else
                f"The previous close was {prev.value} (unadjusted), so no percentage change is shown.")
    med = next((f for f in p.context if f.label.startswith("Median change")), None)
    ud = next((f for f in p.context if f.label == "Stocks up / down"), None)
    market = ([f"Market that day: {med.label.lower()} {med.value}" + (f"; stocks up / down {ud.value}." if ud else ".")]
              if med else [])
    flows = [f"{f.label} {f.value}" for f in p.context if "net buying" in f.label]
    return head, market, (["; ".join(flows) + "."] if flows else [])


def _move_lines(p: Packet, who: str, summary=None, notes=(), shown_out=None):
    head, market, flows = _move_head(p, who)
    gaps, factors = _gap_lines(p), shown_candidates(p)
    picked = []

    def factor_block(k, with_plus):
        shown = select_factors(factors, k) if k else []
        picked[:] = shown
        if not shown:
            return []
        if len(shown) == 1:                                   # header and item merged into one line
            more = f" (plus {len(factors) - 1} more not shown)" if with_plus and len(factors) > 1 else ""
            return ["Possible contributing factor (not a confirmed cause), " + _factor_line(shown[0])[2:] + more]
        extra = [f"(plus {len(factors) - len(shown)} more not shown)"] if with_plus and len(factors) > len(shown) else []
        return ["Possible contributing factors (not confirmed causes):"] + [_factor_line(f) for f in shown] + extra

    # preference order: keep the factors and the context; give up the extras first; the caveats are never cut
    plans = [(k, plus, True, True) for k in range(MAX_FACTORS_SHOWN, 0, -1) for plus in (True, False)]
    plans += [(1, False, True, False), (1, False, False, False), (0, False, False, False)]
    for k, plus, with_market, with_flows in plans:
        core = (list(summary) if summary is not None else
                head + (market if with_market else []) + (flows if with_flows else []))
        lines = core + list(notes) + factor_block(k, plus)
        if len(lines) + len(gaps) <= MAX_LINES:
            break
    if shown_out is not None:
        shown_out[:] = picked
    return lines + gaps


def _return_lines(p: Packet, who: str):
    start, end, ret = p.facts[0], p.facts[1], next(f for f in p.facts if f.label == "Price return")
    lines = [f"{who} price return from {human_date(start.date)} to {human_date(end.date)}: {ret.value}.",
             f"Official close {start.value} on {human_date(start.date)}; {end.value} on {human_date(end.date)}."]
    divs = [f for f in p.facts if f.label.startswith("Dividend")]
    lines += [f"{f.label}: {f.value}." for f in divs[:3]]
    return lines + _gap_lines(p)


def _side(fact) -> str:
    """'Tata Consultancy Services Limited (TCS): Price return' -> the name in front of the label."""
    return fact.label.rsplit(": ", 1)[0]


def _compare_lines(p: Packet):
    a, b, diff = p.facts
    sa, ea = p.context[0], p.context[1]
    sb, eb = p.context[2], p.context[3]
    na, nb = _side(a), _side(b)
    lines = [f"From {human_date(sa.date)} to {human_date(ea.date)}: {na} {a.value}; {nb} {b.value}."]
    if diff.raw == 0:
        lines.append(f"The difference is {diff.value} percentage points.")
    else:
        lines.append(f"{na if diff.raw > 0 else nb} was higher by {diff.value} percentage points.")
    lines.append(f"{na}: {sa.value} on {human_date(sa.date)}; {ea.value} on {human_date(ea.date)}.")
    lines.append(f"{nb}: {sb.value} on {human_date(sb.date)}; {eb.value} on {human_date(eb.date)}.")
    return lines + _gap_lines(p)


def _index_return_lines(p: Packet, who: str):
    start, end, chg = p.facts
    return [f"{who} change in level from {human_date(start.date)} to {human_date(end.date)}: {chg.value}.",
            f"Closing level {start.value} on {human_date(start.date)}; {end.value} on {human_date(end.date)}."
            ] + _gap_lines(p)


def _index_lines(p: Packet, who: str, notes=()):
    by = {f.label: f for f in p.facts}
    close = p.facts[0]
    chg, pts = main_change(p), by.get("Points change")
    if chg is not None and pts is not None and chg.raw is not None and pts.raw is not None:
        if chg.raw == 0 and pts.raw == 0:
            move = "unchanged from the previous close"
        else:
            move = f"{'up' if pts.raw > 0 else 'down'} {abs(pts.raw):,.2f} points ({chg.value}) from the previous close"
        first = f"{who} closed at {close.value} on {human_date(close.date)}, {move}."
    else:
        first = f"{who} closed at {close.value} on {human_date(close.date)}."
    lines = [first]
    rng = [f"{label.lower()} {by[label].value}" for label in ("Open", "High", "Low") if label in by]
    if rng:
        lines.append("; ".join(rng).capitalize() + ".")
    val = [f"{label if label != 'Dividend yield' else 'dividend yield'} {by[label].value}"
           for label in ("P/E", "P/B", "Dividend yield") if label in by]
    if val:
        lines.append("NSE publishes " + "; ".join(val) + ".")
    return lines + list(notes) + _gap_lines(p)


def _events_lines(p: Packet, who: str, shown_out=None):
    lines = [f"{who}: recent exchange filings and what is due."]
    lines += [f"{f.label}: {f.value}." for f in p.facts]
    items = [f for f in p.factors][: MAX_LINES - 1 - len(p.facts) - len(p.gaps)]
    if shown_out is not None:
        shown_out[:] = items
    lines += [_factor_line(f) for f in items]
    if not p.factors:
        lines.append("No recent filings or meetings were found.")
    return lines + _gap_lines(p)


# ------------------------------------------------------------------ footer (code only)
def footer_links(p: Packet, limit: int = 3, factors=None):
    out = []
    for f in (p.factors if factors is None else factors):
        if f.url:
            label = _clip(f.title, 60) + (f" ({f.source.split(': ', 1)[-1]})" if f.kind == "news" else "")
            out.append((label, f.url))
    return out[:limit]


def build_footer(p: Packet, html_links: bool = False, factors=None) -> str:
    """Sources, links and the as-of line, all from the packet. html_links=True writes Telegram HTML anchors so long
    links stay hidden behind a label; plain text omits links longer than MAX_LINK_CHARS.
    `factors` is the list the message actually displayed; sources and links describe only those."""
    shown = list(p.factors if factors is None else factors)
    seen, sources = set(), []
    for item in list(p.facts) + list(p.context) + shown:
        src = "Google News headlines" if item.source.startswith("News:") else item.source
        if src not in seen:
            seen.add(src)
            sources.append(src)
    if any(g.code == "corporate_action_on_date" for g in p.gaps) and "NSE corporate actions" not in seen:
        sources.append("NSE corporate actions")                 # stated in the caveat even when not listed as a factor
    lines = []
    if sources:
        src_line = "Sources: " + "; ".join(sources)
        lines.append(html.escape(src_line, quote=False) if html_links else src_line)
    for label, url in footer_links(p, 3, shown):
        if html_links:
            lines.append(f'Link: <a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        elif len(url) <= MAX_LINK_CHARS:
            lines.append(f"Link: {label} {url}")
        else:
            lines.append(f"Link: {label} (long link not shown in plain text)")
    prices = any(f.source.startswith(PRICE_SOURCE) or f.source == INDEX_SOURCE for f in list(p.facts) + list(p.context))
    index = any(f.source == INDEX_SOURCE for f in list(p.facts) + list(p.context))
    stock = any(f.source.startswith(PRICE_SOURCE) for f in list(p.facts) + list(p.context))
    price_note = ("Prices and index levels are NSE official end-of-day values, not live." if index and stock
                  else "Index levels are NSE official end-of-day values, not live." if index
                  else "Prices are NSE official end-of-day closes, not live quotes.")
    try:
        stamp = datetime.fromisoformat(p.as_of).astimezone(IST)
        lines.append(f"As of {stamp.day} {MONTHS[stamp.month - 1]} {stamp.year}, {stamp:%H:%M} IST." + (f" {price_note}" if prices else ""))
    except (ValueError, TypeError):
        if prices:
            lines.append(price_note)
    lines.append("Information only, not investment advice.")
    return "\n".join(lines)


# ------------------------------------------------------------------ summary prompt (the only model-written part)
SUMMARY_SYSTEM = """You write the opening summary of a short phone message about one stock's price move. English only.
Rules:
1. Use ONLY the facts in the packet. Never add numbers, dates, companies, causes, sources or opinions.
2. Copy every number exactly as written in the packet. Do not round, convert or recompute.
3. Write 2 or 3 short sentences of plain text: no markdown, no lists, no emojis, no all-caps words, no clock times.
4. State the company and the official close together with its date, then the change from the previous close,
   then the market context if the packet has it.
5. Do not mention filings, announcements, news, headlines or possible factors: they are added separately.
6. Never explain why the price moved. Never give advice or predictions.
7. If the packet shows no 'Change from previous close', do not describe the price as up, down, higher or lower."""


def summary_input(p: Packet) -> str:
    facts = [{"label": f.label, "value": f.value, **({"date": human_date(f.date)} if f.date else {})} for f in p.facts]
    ctx = [{"label": f.label, "value": f.value} for f in p.context]
    payload = {"company": p.name, "symbol": p.symbol, "facts": facts, "market_context": ctx}
    return "Packet:\n" + json.dumps(payload, ensure_ascii=False) + "\nWrite the summary now."
