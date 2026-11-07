"""Symbol resolver: turns what a person types into an NSE symbol, or asks.

Universe comes from NSE's daily end-of-day file (bhavcopy), which includes ETFs
such as GOLDBEES. Pure logic, no network. Never guesses silently: low
confidence returns candidates so the bot can ask "Did you mean ...?".
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

# Series we treat as tradable equities/ETFs. UNVERIFIED: confirm with --stats on real data.
ALLOWED_SERIES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})
NAME_STOPWORDS = frozenset({"ltd", "lt", "limited", "co", "company", "pvt", "private", "the"})

AUTO_ACCEPT_SCORE = 0.85   # fuzzy/typo matches at or above this may be accepted ...
AUTO_ACCEPT_MARGIN = 0.07  # ... only if clearly ahead of the runner-up
MIN_CANDIDATE_SCORE = 0.60
CANDIDATE_WINDOW = 0.12    # only list candidates this close to the best score


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    isin: str = ""
    series: str = "EQ"
    turnover: float = 0.0
    kind: str = "equity"   # "equity" (incl. ETFs) or "index"


@dataclass(frozen=True)
class Match:
    instrument: Instrument
    score: float


@dataclass(frozen=True)
class Resolution:
    status: str            # "exact" | "confident" | "ambiguous" | "none"
    query: str
    matches: tuple        # best first, at most 3

    @property
    def best(self):
        return self.matches[0].instrument if self.matches else None


# Index names must equal the "index" field of NSE's allIndices feed.
# "NIFTY 50" is verified (spike, Nov 2026); "NIFTY BANK" is UNVERIFIED.
INDICES = (
    Instrument("NIFTY 50", "NIFTY 50", kind="index"),
    Instrument("NIFTY BANK", "NIFTY BANK", kind="index"),
)
INDEX_ALIASES = {"nifty": "NIFTY 50", "nifty50": "NIFTY 50", "nifty 50": "NIFTY 50",
                 "banknifty": "NIFTY BANK", "bank nifty": "NIFTY BANK",
                 "nifty bank": "NIFTY BANK"}


def normalize(text: str) -> str:
    t = text.lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(w for w in t.split() if w not in NAME_STOPWORDS)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _tok_match(w: str, t: str, allow_abbrev: bool) -> bool:
    """Query word w vs name word t. NSE abbreviates names (SERV, INST, NIP), so for
    multi-word queries a name word may be a prefix of the query word. A single
    mistyped word never gets this leniency (infosis must not match INFO EDGE)."""
    if w == t:
        return True
    if len(w) >= 3 and t.startswith(w):
        return True
    return allow_abbrev and len(t) >= 3 and w.startswith(t)


def _score(q: str, qc: str, inst: Instrument) -> float:
    sym = re.sub(r"[^a-z0-9]", "", inst.symbol.lower())
    name = normalize(inst.name)
    nc = name.replace(" ", "")
    tokens = [t for t in name.split() if len(t) > 1]
    qtok = q.split()
    if qc == sym:
        return 1.0
    if q == name or qc == nc:
        return 0.99
    if len(qc) >= 3 and nc.startswith(qc):
        return 0.90 + 0.08 * len(qc) / len(nc)
    if len(qtok) >= 1 and all(any(_tok_match(w, t, len(qtok) >= 2) for t in tokens) for w in qtok) and len(qc) >= 3:
        return 0.85 + 0.08 * len(qtok) / max(len(tokens), 1)
    if len(qc) >= 4 and qc in nc:
        return 0.80 + 0.08 * len(qc) / len(nc)
    if len(qc) >= 4:
        best = max(_ratio(qc, sym), 0.95 * _ratio(qc, nc),
                   max((0.9 * _ratio(qc, t) for t in tokens if len(t) >= 4), default=0.0))
        return best
    return 0.0


class Resolver:
    def __init__(self, instruments):
        # one entry per symbol, keep the most traded series
        best: dict[str, Instrument] = {}
        for i in instruments:
            cur = best.get(i.symbol)
            if cur is None or i.turnover > cur.turnover:
                best[i.symbol] = i
        self._all = list(best.values()) + list(INDICES)

    def resolve(self, query: str) -> Resolution:
        q = normalize(query)
        qc = q.replace(" ", "")
        if not qc:
            return Resolution("none", query, ())
        alias = INDEX_ALIASES.get(q) or INDEX_ALIASES.get(qc)
        if alias:
            inst = next(i for i in INDICES if i.symbol == alias)
            return Resolution("exact", query, (Match(inst, 1.0),))
        scored = [(s, i) for i in self._all if (s := _score(q, qc, i)) >= MIN_CANDIDATE_SCORE]
        scored.sort(key=lambda x: (-round(x[0], 6), -x[1].turnover, x[1].symbol))
        if not scored:
            return Resolution("none", query, ())
        cutoff = max(MIN_CANDIDATE_SCORE, scored[0][0] - CANDIDATE_WINDOW)
        top = tuple(Match(i, round(s, 3)) for s, i in scored if s >= cutoff)[:3]
        first = top[0].score
        second = top[1].score if len(top) > 1 else 0.0
        if first >= 1.0 and second < 1.0:
            return Resolution("exact", query, top)
        if first >= AUTO_ACCEPT_SCORE and first - second >= AUTO_ACCEPT_MARGIN:
            return Resolution("confident", query, top)
        return Resolution("ambiguous", query, top)


def parse_bhavcopy(text: str):
    """Return (instruments, stats) from the CSV text of NSE's CM bhavcopy."""
    rows = list(csv.DictReader(io.StringIO(text)))
    stats = {"rows": len(rows),
             "series": Counter(r.get("SctySrs", "") for r in rows),
             "instrument_types": Counter(r.get("FinInstrmTp", "") for r in rows)}
    out = []
    for r in rows:
        if r.get("SctySrs") not in ALLOWED_SERIES or not r.get("TckrSymb"):
            continue
        try:
            turnover = float(r.get("TtlTrfVal") or 0)
        except ValueError:
            turnover = 0.0
        out.append(Instrument(r["TckrSymb"].strip(), (r.get("FinInstrmNm") or "").strip(),
                              (r.get("ISIN") or "").strip(), r["SctySrs"], turnover))
    return out, stats


def format_choices(res: Resolution) -> str:
    """Code-built wording for a low-confidence match. No model involved."""
    if res.status == "none":
        return f"I couldn't find a stock or ETF matching \"{res.query}\". Try the NSE symbol."
    if res.status == "ambiguous":
        opts = "; ".join(f"{m.instrument.name} ({m.instrument.symbol})" for m in res.matches)
        if len(res.matches) == 1:
            return f"Did you mean {opts}? Reply yes, or send the NSE symbol."
        return f"Did you mean: {opts}? Reply with the symbol."
    b = res.matches[0].instrument
    return f"Showing {b.name} ({b.symbol})."
