"""From a verified packet to a message: deterministic text, model-written text, validator, footer.

The model may only REPHRASE a packet that code built. validate_text() rejects any model text that contains a
number, date, clock time or ticker not found in the packet, causal wording, advice or predictions, a wrong
direction, a missing key figure, a missing required caveat, or more than MAX_LINES lines. Rejected text is
replaced by fallback_text(), which is built from the packet alone and passes the same validator.
English only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from briany.evidence import IST, MONTHS, Packet, human_date

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
def validate_text(text: str, p: Packet) -> list:
    """Problems found in `text` against the packet (empty list = acceptable)."""
    problems = []
    text = (text or "").strip()
    lines = [l for l in text.splitlines() if l.strip()]
    if not text:
        return ["empty text"]
    if len(lines) > MAX_LINES or len(text) > MAX_CHARS:
        problems.append(f"too long ({len(lines)} lines, {len(text)} characters)")
    low = text.lower()
    blob = packet_blob(p)

    if CAUSAL.search(text):
        problems.append(f"causal wording: {CAUSAL.search(text).group(0)!r}")
    if ADVICE.search(text):
        problems.append(f"advice or prediction: {ADVICE.search(text).group(0)!r}")
    if p.kind == "move" and p.factors and not POSSIBLE.search(text):
        problems.append("factors must be described as possible contributing factors")

    # dates: every mentioned date must be a packet date (with year, when a year is given)
    packet_dates = _dates_in(blob) | {tuple(int(x) for x in f.date.split("-")) for f in p.facts + p.context if f.date}
    packet_dates |= {tuple(int(x) for x in f.when[:10].split("-")) for f in p.factors if f.when}
    for y, m, d in _dates_in(text):
        if not any(pm == m and pd == d and (y is None or py is None or py == y) for py, pm, pd in packet_dates):
            problems.append(f"date not in the data: {d} {MONTHS[m - 1]}{' ' + str(y) if y else ''}")
    if CLOCK.search(text):
        problems.append(f"clock time {CLOCK.search(text).group(0)!r}: times are added by code, not the text")

    # numbers
    body = BULLET.sub("", _strip_dates(URL.sub(" ", text)))
    allowed = _allowed_numbers(p, blob)
    seen = set()
    for tok in NUMBER.findall(body):
        d = _dec(tok)
        if d is not None:
            seen.add(d)
            if d not in allowed:
                problems.append(f"number not in the data: {tok.strip()}")
    chg = main_change(p)
    if p.kind == "move" and p.status == "ok":
        close = p.facts[0] if p.facts else None
        for need in ([close] if close else []) + ([chg] if chg else []):
            if need.raw is not None and abs(Decimal(str(need.raw))) not in seen and _dec(need.value) not in seen:
                problems.append(f"missing key figure: {need.label}")
    if p.kind == "return" and p.status == "ok":
        ret = next((f for f in p.facts if f.label == "Price return"), None)
        if ret and _dec(ret.value) not in seen:
            problems.append("missing key figure: Price return")

    # direction must agree with the sign of the change
    if chg and chg.raw is not None and chg.raw != 0:
        target = abs(Decimal(str(chg.raw)))
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            if any(_dec(t) == target for t in NUMBER.findall(sentence)):
                if chg.raw > 0 and DOWN_WORDS.search(sentence) and not UP_WORDS.search(sentence):
                    problems.append("direction contradicts the data (the price rose)")
                if chg.raw < 0 and UP_WORDS.search(sentence) and not DOWN_WORDS.search(sentence):
                    problems.append("direction contradicts the data (the price fell)")

    # tickers / all-caps names must come from the packet
    blob_low = blob.lower()
    for tok in set(ALLCAPS.findall(text)):
        if tok not in WHITELIST_CAPS and not re.search(rf"(?<![a-z0-9]){re.escape(tok.lower())}(?![a-z0-9])", blob_low):
            problems.append(f"name or ticker not in the data: {tok}")

    for code, phrase in ((g.code, REQUIRED_PHRASES[g.code]) for g in p.gaps if g.code in REQUIRED_PHRASES):
        if phrase not in low:
            problems.append(f"required caveat missing ({code}): include {phrase!r}")
    return problems


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
    return f"- {label}, {f.timing}: {_clip(f.title)}"


def _gap_lines(p: Packet, skip=()):
    out = []
    for g in p.gaps:
        if g.code in skip:
            continue
        out.append(f"No clear evidence found. {g.text}" if g.code == "no_clear_evidence" else g.text)
    return out


def fallback_text(p: Packet) -> str:
    """Built from the packet alone. Passes validate_text() by construction (tested for every packet type)."""
    if p.status == "clarify":
        return p.gaps[0].text if p.gaps else "Which company do you mean? Please give the company name or NSE symbol."
    who = f"{p.name} ({p.symbol})" if p.symbol else "this company"
    if p.status == "refused":
        lines = [f"I can't give a reliable figure for {who}."] + _gap_lines(p)
        lines += [f"- {f.label}: {f.value}" + (f" ({human_date(f.date)})" if f.date else "") for f in p.facts[:3]]
        return "\n".join(lines[:MAX_LINES])
    if p.kind == "move":
        return "\n".join(_move_lines(p, who)[:MAX_LINES])
    if p.kind == "return":
        return "\n".join(_return_lines(p, who)[:MAX_LINES])
    return "\n".join(_events_lines(p, who)[:MAX_LINES])


def _move_lines(p: Packet, who: str):
    close, prev, chg = p.facts[0], p.facts[1], main_change(p)
    head = [f"{who} closed at {close.value} on {human_date(close.date)}."]
    head.append(f"That is {chg.value} from the previous close of {prev.value}." if chg else
                f"The previous close was {prev.value} (unadjusted), so no percentage change is shown.")
    med = next((f for f in p.context if f.label.startswith("Median change")), None)
    ud = next((f for f in p.context if f.label == "Stocks up / down"), None)
    market = ([f"Market that day: {med.label.lower()} {med.value}" + (f"; stocks up / down {ud.value}." if ud else ".")]
              if med else [])
    flows = [f"{f.label} {f.value}" for f in p.context if "net buying" in f.label]
    flows = ["; ".join(flows) + "."] if flows else []
    gaps = _gap_lines(p)

    def factor_block(k, with_plus):
        shown = select_factors(p.factors, k) if k else []
        if not shown:
            return []
        if len(shown) == 1:                                   # header and item merged into one line
            line = _factor_line(shown[0])[2:]
            more = f" (plus {len(p.factors) - 1} more not shown)" if with_plus and len(p.factors) > 1 else ""
            return ["Possible contributing factor (not a confirmed cause), " + line + more]
        extra = [f"(plus {len(p.factors) - len(shown)} more not shown)"] if with_plus and len(p.factors) > len(shown) else []
        return ["Possible contributing factors (not confirmed causes):"] + [_factor_line(f) for f in shown] + extra

    # preference order: keep the factors and the context; give up the extras first; the caveats are never cut
    plans = [(k, plus, True, True) for k in range(MAX_FACTORS_SHOWN, 0, -1) for plus in (True, False)]
    plans += [(1, False, True, False), (1, False, False, False), (0, False, False, False)]
    for k, plus, with_market, with_flows in plans:
        lines = head + (market if with_market else []) + (flows if with_flows else []) + factor_block(k, plus)
        if len(lines) + len(gaps) <= MAX_LINES:
            break
    return lines + gaps


def _return_lines(p: Packet, who: str):
    start, end, ret = p.facts[0], p.facts[1], next(f for f in p.facts if f.label == "Price return")
    lines = [f"{who} price return from {human_date(start.date)} to {human_date(end.date)}: {ret.value}.",
             f"Official close {start.value} on {human_date(start.date)}; {end.value} on {human_date(end.date)}."]
    divs = [f for f in p.facts if f.label.startswith("Dividend")]
    lines += [f"{f.label}: {f.value}." for f in divs[:3]]
    return lines + _gap_lines(p)


def _events_lines(p: Packet, who: str):
    lines = [f"{who}: recent exchange filings and what is due."]
    lines += [f"{f.label}: {f.value}." for f in p.facts]
    items = [f for f in p.factors][: MAX_LINES - 1 - len(p.facts) - len(p.gaps)]
    lines += [_factor_line(f) for f in items]
    if not p.factors:
        lines.append("No recent filings or meetings were found.")
    return lines + _gap_lines(p)


# ------------------------------------------------------------------ footer (code only)
def build_footer(p: Packet) -> str:
    seen, sources = set(), []
    for item in list(p.facts) + list(p.context) + list(p.factors):
        src = "Google News headlines" if item.source.startswith("News:") else item.source
        if src not in seen:
            seen.add(src)
            sources.append(src)
    lines = []
    if sources:
        lines.append("Sources: " + "; ".join(sources))
    links = [f for f in p.factors if f.url][:3]
    lines += [f"Link: {_clip(f.title, 60)} {f.url}" for f in links]
    try:
        stamp = datetime.fromisoformat(p.as_of).astimezone(IST)
        lines.append(f"As of {stamp.day} {MONTHS[stamp.month - 1]} {stamp.year}, {stamp:%H:%M} IST. "
                     "Prices are NSE official end-of-day closes, not live quotes.")
    except (ValueError, TypeError):
        lines.append("Prices are NSE official end-of-day closes, not live quotes.")
    lines.append("Information only, not investment advice.")
    return "\n".join(lines)


# ------------------------------------------------------------------ writer prompt
WRITER_SYSTEM = """You turn a verified data packet into a short message for a phone. English only.
Hard rules:
1. Use ONLY facts in the packet. Never add numbers, dates, companies, causes, sources or opinions.
2. Copy every number exactly as written in the packet (same digits). Do not round, convert or recompute.
3. Write 4 to 8 short lines of plain text: no markdown, no numbering, no emojis, no all-caps words, no clock times.
4. Say the company name and the official close, then the change, then market context if present.
5. Factors are only POSSIBLE contributing factors, never causes. Introduce them with 'Possible contributing factors
   (not confirmed causes):' and name each by type and timing. Show at most 4. Do not use words like because, due to,
   caused by, driven by, led to, thanks to.
6. Never give advice or predictions (no should, buy, sell, will rise, bullish, target).
7. You MUST include each phrase listed in must_include, exactly, in your message.
8. Do not write sources, links or timestamps; they are added separately."""


def compact_packet(p: Packet, max_factors: int = 5) -> dict:
    return {"company": p.name, "symbol": p.symbol, "kind": p.kind,
            "facts": [{"label": f.label, "value": f.value} for f in p.facts],
            "context": [{"label": f.label, "value": f.value} for f in p.context],
            "factors": [{"type": f.kind, "when": human_date(f.when), "timing": f.timing, "title": _clip(f.title, 100),
                         "source": f.source} for f in select_factors(p.factors, max_factors)],
            "caveats": [g.text for g in p.gaps],
            "must_include": required_phrases(p)}


def writer_input(p: Packet) -> str:
    return "Packet:\n" + json.dumps(compact_packet(p), ensure_ascii=False) + "\nWrite the message now."
