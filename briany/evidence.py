"""Evidence assembler: turns a question into a verified packet. Code builds every fact, number,
date and source line; a language model later only phrases the packet and never adds to it.

Rules (decided, not guessed):
 * A move gets a possible factor only if a dated, sourced item falls between the previous close and
   this close: an NSE filing, a corporate action ex-dated in that interval, a board meeting on the
   day, or news published in that interval. Factors are labelled by type and time, never as causes.
 * A failed source is a gap, never "nothing found". "No clear evidence found" is only produced when
   every source was checked successfully and none qualified.
 * Missing or stale corporate actions, an unlisted large move, a corporate action in a return window,
   or missing history all REFUSE the number instead of guessing.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from briany.marketdata import (JUMP_PCT, MIN_JUMP_PRICE, daily_move, dividends_in, load_blockers,
                               period_return, period_start)
from briany.news import first_token_is_unique
from briany.nse import NseError
from briany.symbols import format_choices, is_entitlement

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)
NEWS_DAYS = 6                 # Google News search only reaches back about a week
STALE_AFTER_DAYS = 4          # tunable once the publication time of NSE's file is measured
MIN_BREADTH_STOCKS = 200
PRICE_SOURCE = "NSE end-of-day file"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# gaps that mean "a source could not be checked": they forbid saying "no clear evidence found"
CHECK_FAILURES = frozenset({"announcements_unavailable", "board_meetings_unavailable", "news_unavailable",
                            "news_not_covered"})


@dataclass(frozen=True)
class Fact:
    label: str
    value: str                    # display text, built by code
    raw: float | None = None      # number behind the display text, for the answer validator
    source: str = PRICE_SOURCE
    date: str | None = None


@dataclass(frozen=True)
class Factor:
    kind: str                     # announcement | corporate_action | board_meeting | news
    when: str                     # ISO date or datetime (IST)
    timing: str
    title: str
    source: str
    detail: str = ""
    url: str | None = None


@dataclass(frozen=True)
class Gap:
    code: str
    text: str
    level: str = "limit"          # "info" (a caveat), "limit" (restricts a claim), "missing" (could not check)


@dataclass(frozen=True)
class Packet:
    kind: str                     # move | return | events | clarify
    status: str                   # ok | refused | clarify
    symbol: str = ""
    name: str = ""
    as_of: str = ""
    facts: tuple = ()
    context: tuple = ()
    factors: tuple = ()
    gaps: tuple = ()
    evidence_complete: bool = False
    no_clear_evidence: bool = False
    choices: tuple = ()

    def to_dict(self) -> dict:
        return asdict(self)

    def numbers(self) -> list:
        return [f.raw for f in self.facts + self.context if f.raw is not None]


# ------------------------------------------------------------------ formatting
def money(x: float) -> str:
    return f"₹{x:,.2f}"


def pct_text(x: float) -> str:
    return f"{x:+.2f}%"


def human_date(iso: str) -> str:
    d = Date.fromisoformat(iso[:10])
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _at(day: str, t: time) -> datetime:
    return datetime.combine(Date.fromisoformat(day), t, tzinfo=IST)


def _prev_day(day: str) -> str:
    return (Date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _aware(iso: str) -> datetime:
    t = datetime.fromisoformat(iso)
    return t if t.tzinfo else t.replace(tzinfo=IST)


class Assembler:
    def __init__(self, store, nse, news, resolver, now=None):
        self.store, self.nse, self.news, self.resolver = store, nse, news, resolver
        self._now = now or (lambda: datetime.now(IST))

    # ---------------------------------------------------------------- shared helpers
    def _stamp(self) -> str:
        return self._now().astimezone(IST).isoformat(timespec="seconds")

    def _today(self) -> str:
        return self._now().astimezone(IST).date().isoformat()

    def _resolve(self, query):
        res = self.resolver.resolve(query)
        if res.status in ("exact", "confident"):
            inst = res.best
            if inst.kind == "index":
                return None, Packet("clarify", "refused", inst.symbol, inst.name, self._stamp(), gaps=(Gap(
                    "index_not_supported", "Index levels are not stored; only stocks and ETFs are supported.",
                    "missing"),))
            return inst, None
        return None, Packet("clarify", "clarify", as_of=self._stamp(),
                            gaps=(Gap("symbol_unresolved", format_choices(res), "missing"),),
                            choices=tuple(f"{m.instrument.name} ({m.instrument.symbol})" for m in res.matches))

    def _refuse(self, kind, inst, *gaps, facts=()):
        return Packet(kind, "refused", inst.symbol, inst.name, self._stamp(), facts=tuple(facts), gaps=tuple(gaps))

    def _stale_gap(self, day):
        if (Date.fromisoformat(self._today()) - Date.fromisoformat(day)).days > STALE_AFTER_DAYS:
            return [Gap("data_may_be_stale", f"The latest stored close is {human_date(day)}; "
                        "newer data may exist.", "limit")]
        return []

    def market_breadth(self, day):
        """Context built from the stored day itself: median change across listed stocks. Not an index."""
        changes = []
        for sym, row in self.store.get_day(day).items():
            if not row.prev_close or row.prev_close < MIN_JUMP_PRICE or is_entitlement(sym):
                continue
            pct = (row.close - row.prev_close) / row.prev_close * 100
            if abs(pct) < JUMP_PCT:
                changes.append(pct)
        if len(changes) < MIN_BREADTH_STOCKS:
            return []
        med = round(statistics.median(changes), 2)
        up, down = sum(1 for c in changes if c > 0), sum(1 for c in changes if c < 0)
        src = f"{PRICE_SOURCE} (computed)"
        return [Fact(f"Median change across {len(changes)} listed stocks", pct_text(med), med, src, day),
                Fact("Stocks up / down", f"{up} / {down}", None, src, day)]

    # ---------------------------------------------------------------- why did it move
    def why_moved(self, query, on=None) -> Packet:
        inst, clar = self._resolve(query)
        if clar:
            return clar
        sym, today = inst.symbol, self._today()
        blockers, status = load_blockers(self.store, sym, today)
        if status != "ok":
            return self._refuse("move", inst, Gap("actions_unavailable", "Corporate actions could not be verified "
                                f"({status.split(': ', 1)[-1]}), so no price change is given.", "missing"))
        move = daily_move(self.store, sym, on, actions=blockers)
        if move.status == "no_data":
            return self._refuse("move", inst, Gap("no_price_data", move.note or "No price data for that day.", "missing"))
        day = move.date
        prior = self.store.day_on_or_before(_prev_day(day))
        facts = [Fact(f"Official close, {human_date(day)}", money(move.close), move.close, PRICE_SOURCE, day),
                 Fact("Previous close" + (" (unadjusted)" if move.status == "blocked_by_corporate_action" else ""),
                      money(move.prev_close), move.prev_close, PRICE_SOURCE, prior or None)]
        gaps = self._stale_gap(day) if on is None else []
        if move.status != "blocked_by_corporate_action":
            facts.append(Fact("Change from previous close", pct_text(move.pct), move.pct, PRICE_SOURCE, day))
        if move.status == "blocked_by_corporate_action":
            subjects = "; ".join(a.purpose for a in move.actions)
            gaps.append(Gap("corporate_action_on_date", f"A corporate action took effect on this date ({subjects}); "
                            "NSE's previous close is not adjusted, so the raw change is not a real price move.", "limit"))
        elif move.status == "unverified_large_move":
            gaps.append(Gap("unverified_large_move", f"The price changed by {pct_text(move.pct)} but NSE's data lists no "
                            "matching corporate action; it may be an unlisted split or unit change.", "limit"))

        context = self.market_breadth(day)
        if day == self.store.latest_day():
            try:
                for r in (x for x in self.nse.fii_dii() if x.date == day):
                    context.append(Fact(f"{r.category} net buying/selling (Rs crore)", f"{r.net:+,.2f}", r.net,
                                        "NSE FII/DII data", r.date))
            except NseError:
                gaps.append(Gap("flows_unavailable", "FII/DII figures could not be fetched.", "missing"))

        lo = _at(prior, CLOSE) if prior else _at(_prev_day(day), CLOSE)
        hi = _at(day, CLOSE)
        opening = _at(day, OPEN)

        def timing(t):
            return "after the previous close, before the open" if t < opening else "during market hours"

        factors, name = [], inst.name
        try:                                                     # filings; the wide window also gives the full name
            anns = self.nse.announcements(sym, (Date.fromisoformat(day) - timedelta(days=45)).isoformat(), day)
            if anns:
                name = anns[0].company or name
            for a in anns:
                t = _aware(a.published)
                if lo < t <= hi:
                    factors.append(Factor("announcement", a.published, timing(t), a.category, "NSE announcement",
                                          a.text[:200], a.url))
        except NseError:
            gaps.append(Gap("announcements_unavailable", "NSE announcements could not be checked.", "missing"))
        for a in self.store.get_actions(sym):
            if (prior is None or a.ex_date > prior) and a.ex_date <= day:
                factors.append(Factor("corporate_action", a.ex_date, "ex-date", a.subject, "NSE corporate actions"))
        try:
            seen = set()
            for m in self.nse.board_meetings(sym, prior or _prev_day(day), day):
                if (prior or _prev_day(day)) <= m.meeting_date <= day and m.meeting_date not in seen:
                    seen.add(m.meeting_date)
                    label = "Results board meeting" if m.results_related else "Board meeting"
                    factors.append(Factor("board_meeting", m.meeting_date, "meeting date",
                                          f"{label}: {m.purpose[:120]}", "NSE board meetings"))
        except NseError:
            gaps.append(Gap("board_meetings_unavailable", "NSE board meetings could not be checked.", "missing"))

        if (Date.fromisoformat(today) - Date.fromisoformat(day)).days <= NEWS_DAYS:
            res = self.news.fetch(name, sym, now=self._now(), days=7, limit=10,
                                  unique_first=first_token_is_unique(self.resolver, name))
            if res.status == "error":
                gaps.append(Gap("news_unavailable", "News could not be checked.", "missing"))
            else:
                for it in res.items:
                    t = _aware(it.published)
                    if lo < t <= hi:
                        factors.append(Factor("news", it.published, timing(t), it.title, f"News: {it.source}",
                                              url=it.url))
        else:
            gaps.append(Gap("news_not_covered", "News search only covers the last week, so news for this "
                            "date was not checked.", "missing"))

        factors.sort(key=lambda f: f.when)
        complete = not any(g.code in CHECK_FAILURES for g in gaps)
        none_found = complete and not factors and move.status in ("ok", "unverified_large_move")
        if none_found:
            gaps.append(Gap("no_clear_evidence", f"No filing, corporate action, board meeting or relevant news was "
                            f"found between the {human_date(prior) if prior else 'previous'} close and the "
                            f"{human_date(day)} close.", "info"))
        return Packet("move", "ok", sym, name, self._stamp(), tuple(facts), tuple(context), tuple(factors),
                      tuple(gaps), complete, none_found)

    # ---------------------------------------------------------------- period return
    def period_return(self, query, label=None, start=None, end=None) -> Packet:
        inst, clar = self._resolve(query)
        if clar:
            return clar
        sym, today = inst.symbol, self._today()
        blockers, status = load_blockers(self.store, sym, today)
        if status != "ok":
            return self._refuse("return", inst, Gap("actions_unavailable", "Corporate actions could not be verified "
                                f"({status.split(': ', 1)[-1]}), so no return is given.", "missing"))
        end = end or self.store.latest_day()
        if not end:
            return self._refuse("return", inst, Gap("no_price_data", "No price data is stored.", "missing"))
        target = start or (period_start(end, label) if label else None)
        if not target:
            return self._refuse("return", inst, Gap("bad_period", "Unknown period; use 1w, 1m, 3m, 6m, 1y, ytd "
                                "or give a start date.", "missing"))
        r = period_return(self.store, sym, end, target, actions=blockers)
        if r.status == "ok":
            facts = [Fact(f"Official close, {human_date(r.start_date)}", money(r.start_close), r.start_close,
                          PRICE_SOURCE, r.start_date),
                     Fact(f"Official close, {human_date(r.end_date)}", money(r.end_close), r.end_close,
                          PRICE_SOURCE, r.end_date),
                     Fact("Price return", pct_text(r.pct), r.pct, PRICE_SOURCE, r.end_date)]
            for d, amount in dividends_in(self.store, sym, r.start_date, r.end_date):
                facts.append(Fact(f"Dividend, ex-date {human_date(d)}", money(amount) if amount else "amount not stated",
                                  amount, "NSE corporate actions", d))
            gaps = [Gap("price_return_only", "This is a price return; it excludes dividends.", "info")]
            gaps += self._stale_gap(r.end_date) if end == self.store.latest_day() else []
            return Packet("return", "ok", sym, inst.name, self._stamp(), tuple(facts), (), (), tuple(gaps), True)
        if r.status == "blocked_by_corporate_action":
            facts = [Fact("Corporate action in this period", a.purpose, None, "NSE corporate actions", a.ex_date)
                     for a in r.actions]
            return self._refuse("return", inst, Gap("corporate_action_in_period", "Closes are not adjusted for splits "
                                "or bonuses, so no return is given for this period.", "limit"), facts=facts)
        if r.status == "unverified_large_move":
            facts = [Fact("Large one-day change", pct_text(p), p, PRICE_SOURCE, d) for d, p in r.jumps]
            return self._refuse("return", inst, Gap("unverified_large_move", "A one-day change of 30% or more in this "
                                "period has no matching corporate action; it may be an unlisted split, so no return "
                                "is given.", "limit"), facts=facts)
        code = "insufficient_history" if r.status == "insufficient_history" else "no_price_data"
        return self._refuse("return", inst, Gap(code, r.note or "Not enough price data.", "missing"))

    # ---------------------------------------------------------------- recent events and what is due
    def events(self, query, days=14) -> Packet:
        inst, clar = self._resolve(query)
        if clar:
            return clar
        sym, today = inst.symbol, self._today()
        factors, facts, gaps = [], [], []
        start = (Date.fromisoformat(today) - timedelta(days=days)).isoformat()
        try:
            for a in self.nse.announcements(sym, start, today)[:8]:
                factors.append(Factor("announcement", a.published, f"on {human_date(a.published)}", a.category,
                                      "NSE announcement", a.text[:200], a.url))
        except NseError:
            gaps.append(Gap("announcements_unavailable", "NSE announcements could not be checked.", "missing"))
        try:
            seen = set()
            ahead = (Date.fromisoformat(today) + timedelta(days=60)).isoformat()
            for m in self.nse.board_meetings(sym, start, ahead):
                if m.meeting_date not in seen:
                    seen.add(m.meeting_date)
                    label = "Results board meeting" if m.results_related else "Board meeting"
                    factors.append(Factor("board_meeting", m.meeting_date, f"on {human_date(m.meeting_date)}",
                                          f"{label}: {m.purpose[:120]}", "NSE board meetings"))
        except NseError:
            gaps.append(Gap("board_meetings_unavailable", "NSE board meetings could not be checked.", "missing"))
        if self.store.get_actions_meta():
            for a in self.store.get_actions(sym):
                if a.ex_date >= today:
                    factors.append(Factor("corporate_action", a.ex_date, f"upcoming, ex-date {human_date(a.ex_date)}",
                                          a.subject, "NSE corporate actions"))
        else:
            gaps.append(Gap("actions_unavailable", "Corporate actions have not been loaded.", "missing"))
        try:
            nxt = self.nse.next_results(sym, today)
            if nxt:
                facts.append(Fact("Next results meeting", human_date(nxt.date), None, "NSE event calendar", nxt.date))
            else:
                gaps.append(Gap("next_results_not_announced", "NSE lists no upcoming results meeting for this company "
                                "(that does not mean none is planned).", "info"))
        except NseError:
            gaps.append(Gap("calendar_unavailable", "The NSE event calendar could not be checked.", "missing"))
        factors.sort(key=lambda f: f.when, reverse=True)
        complete = not any(g.code in CHECK_FAILURES | {"calendar_unavailable", "actions_unavailable"} for g in gaps)
        return Packet("events", "ok", sym, inst.name, self._stamp(), tuple(facts), (), tuple(factors), tuple(gaps),
                      complete, complete and not factors and not facts)
