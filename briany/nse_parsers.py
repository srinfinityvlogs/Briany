"""Turn NSE JSON records into plain objects. Pure functions, no network.

Built against real responses captured in Nov 2026 (see tests/fixtures). Anything not
recognised is kept but flagged, never guessed: unknown corporate actions BLOCK returns.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from briany.marketdata import CorporateAction

log = logging.getLogger("briany.nse")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class ShapeError(ValueError):
    """The response did not have the structure we expect."""


# ------------------------------------------------------------------ dates and text
def parse_date(text):
    """'05-Jun-2026' (any case) -> '2026-06-05'; None for '-', '', garbage."""
    m = re.match(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*$", str(text or ""))
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_datetime(text):
    """'06-Nov-2026 19:10:58' or '16-Jan-2025 20:20' -> '2026-11-06T19:10:58'."""
    m = re.match(r"^\s*(\d{1,2}-[A-Za-z]{3}-\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*$", str(text or ""))
    if not m:
        return None
    d = parse_date(m.group(1))
    return f"{d}T{int(m.group(2)):02d}:{m.group(3)}:{m.group(4) or '00'}" if d else None


def to_nse_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}"


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def _records(data, what):
    if not isinstance(data, list):
        raise ShapeError(f"{what}: expected a list, got {type(data).__name__}")
    return [r for r in data if isinstance(r, dict)]


# ------------------------------------------------------------------ corporate actions
# (kind, blocks_returns, regex). Order matters: specific before general.
_RULES = (
    ("bonus", True, r"\bbonus\b"),
    ("split", True, r"split|sub-?\s?division|face value"),
    ("consolidation", True, r"consolidation"),
    ("capital_reduction", True, r"capital reduction|reduction of (share )?capital"),
    ("restructuring", True, r"de-?merger|spin-?off|amalgamation|merger|arrangement"),
    ("rights", True, r"\brights?\b"),
    ("buyback", False, r"buy-?\s?back"),
    ("dividend", False, r"dividend"),
    ("distribution", False, r"\bdistribution\b"),
    ("interest", False, r"\binterest\b|redemption|principal"),
    ("meeting", False, r"annual general meeting|extra-?ordinary general meeting|\bagm\b|\begm\b"),
)


def classify_subject(subject):
    """-> (kind, blocking). Unrecognised subjects are ('other', True): fail closed."""
    s = " ".join(str(subject or "").lower().split())
    for kind, blocking, pattern in _RULES:
        if re.search(pattern, s):
            return kind, blocking
    return "other", True


def parse_detail(kind, subject):
    """Numbers quoted in the subject, for display and (later) adjustment. Empty if absent."""
    s = clean(subject)
    if kind in ("bonus", "rights"):
        m = re.search(r"(\d+)\s*:\s*(\d+)", s)
        return {"ratio": f"{m.group(1)}:{m.group(2)}"} if m else {}
    if kind == "split":
        m = re.search(r"from\s+rs\.?\s*([\d.]+).*?to\s+rs\.?\s*([\d.]+)", s, re.I)
        if m and _num(m.group(1)) and _num(m.group(2)):
            return {"from": _num(m.group(1)), "to": _num(m.group(2))}
        return {}
    if kind == "dividend":
        m = re.search(r"\b(?:rs|re)\.?\s*(\d+(?:\.\d+)?)", s, re.I)
        return {"amount": float(m.group(1))} if m else {}
    return {}


@dataclass(frozen=True)
class CorpAction:
    symbol: str
    company: str
    ex_date: str                 # ISO
    record_date: str | None
    subject: str                 # whitespace-cleaned, wording as NSE gave it
    kind: str
    blocking: bool
    series: str = ""
    isin: str = ""
    detail: dict = field(default_factory=dict, compare=False)

    def as_blocker(self) -> CorporateAction:
        return CorporateAction(self.ex_date, self.subject)


def parse_corporate_actions(data):
    out, skipped = [], 0
    for r in _records(data, "corporate actions"):
        sym, ex = clean(r.get("symbol")), parse_date(r.get("exDate"))
        if not sym or not ex:
            skipped += 1
            continue
        subject = clean(r.get("subject"))
        kind, blocking = classify_subject(subject)
        out.append(CorpAction(sym, clean(r.get("comp")), ex, parse_date(r.get("recDate")), subject, kind,
                              blocking, clean(r.get("series")), clean(r.get("isin")),
                              parse_detail(kind, subject)))
    if skipped:
        log.warning("corporate actions: skipped %d malformed record(s)", skipped)
    return sorted(out, key=lambda a: (a.ex_date, a.symbol))


def blockers(actions):
    """Actions that make a raw price change meaningless, as marketdata.CorporateAction."""
    return [a.as_blocker() for a in actions if a.blocking]


# ------------------------------------------------------------------ announcements, meetings, calendar
@dataclass(frozen=True)
class Announcement:
    symbol: str
    company: str
    published: str               # ISO datetime
    category: str
    text: str
    url: str | None


def parse_announcements(data):
    out = []
    for r in _records(data, "announcements"):
        sym = clean(r.get("symbol"))
        when = parse_datetime(r.get("an_dt")) or parse_datetime(r.get("exchdisstime"))
        if not sym or not when:
            continue
        out.append(Announcement(sym, clean(r.get("sm_name")), when, clean(r.get("desc")),
                                clean(r.get("attchmntText")), clean(r.get("attchmntFile")) or None))
    return sorted(out, key=lambda a: a.published, reverse=True)


@dataclass(frozen=True)
class BoardMeeting:
    symbol: str
    meeting_date: str            # ISO
    purpose: str                 # raw: sometimes a whole sentence
    description: str
    announced: str | None
    results_related: bool


def parse_board_meetings(data):
    out = []
    for r in _records(data, "board meetings"):
        sym, d = clean(r.get("bm_symbol")), parse_date(r.get("bm_date"))
        if not sym or not d:
            continue
        purpose, desc = clean(r.get("bm_purpose")), clean(r.get("bm_desc"))
        out.append(BoardMeeting(sym, d, purpose, desc, parse_datetime(r.get("bm_timestamp")),
                                "result" in (purpose + " " + desc).lower()))
    return sorted(out, key=lambda m: m.meeting_date, reverse=True)


@dataclass(frozen=True)
class EventItem:
    symbol: str
    date: str                    # ISO
    purpose: str
    description: str


def parse_event_calendar(data):
    out = []
    for r in _records(data, "event calendar"):
        sym, d = clean(r.get("symbol")), parse_date(r.get("date"))
        if sym and d:
            out.append(EventItem(sym, d, clean(r.get("purpose")), clean(r.get("bm_desc"))))
    return sorted(out, key=lambda e: e.date)


def next_results_date(events, today: str):
    """Earliest calendar entry on/after `today` whose purpose mentions results; else None."""
    future = [e for e in events if e.date >= today and "result" in e.purpose.lower()]
    return min(future, key=lambda e: e.date) if future else None


# ------------------------------------------------------------------ FII/DII and large deals
@dataclass(frozen=True)
class FiiDii:
    date: str
    category: str                # 'DII' or 'FII/FPI'
    buy: float                   # Rs crore
    sell: float
    net: float


def parse_fii_dii(data):
    out = []
    for r in _records(data, "FII/DII"):
        d, buy, sell, net = parse_date(r.get("date")), _num(r.get("buyValue")), _num(r.get("sellValue")), _num(r.get("netValue"))
        if d and None not in (buy, sell, net) and clean(r.get("category")):
            out.append(FiiDii(d, clean(r.get("category")), buy, sell, net))
    return out


@dataclass(frozen=True)
class Deal:
    kind: str                    # 'bulk' | 'block' | 'short'
    date: str
    symbol: str
    name: str
    client: str
    side: str
    quantity: int
    price: float | None
    remarks: str


def parse_large_deals(data):
    if not isinstance(data, dict):
        raise ShapeError(f"large deals: expected an object, got {type(data).__name__}")
    out = []
    for kind, key in (("bulk", "BULK_DEALS_DATA"), ("block", "BLOCK_DEALS_DATA"), ("short", "SHORT_DEALS_DATA")):
        for r in _records(data.get(key) or [], key):
            d, qty = parse_date(r.get("date")), _num(r.get("qty"))
            if not d or not clean(r.get("symbol")) or qty is None:
                continue
            out.append(Deal(kind, d, clean(r.get("symbol")), clean(r.get("name")), clean(r.get("clientName")),
                            clean(r.get("buySell")), int(qty), _num(r.get("watp")), clean(r.get("remarks")) if r.get("remarks") != "-" else ""))
    return out
