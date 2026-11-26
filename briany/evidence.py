# SPDX-License-Identifier: Apache-2.0
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

Special sessions (a weekend Muhurat or Budget day, or a weekday listed in SPECIAL_SESSION_DAYS) have hours of their own that no
source here gives, so items published on such a calendar day are neither placed before nor after the close and are not listed;
the answer names the day (see briany/sessions.py). Known limit: a weekday special session NOT in that list is treated as normal.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from briany.marketdata import (JUMP_PCT, LISTING_LOOKBACK, MAX_GAP_DAYS, MIN_JUMP_PRICE, daily_move, dividends_in, load_blockers,
                               period_return, period_start)
from briany.news import first_token_is_unique
from briany.indexfile import find_row
from briany.nse import NseError
from briany.sessions import is_special_session, special_days_from_env
from briany.symbols import find_index, format_choices, is_entitlement

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)
NEWS_DAYS = 6                 # Google News search only reaches back about a week
STALE_AFTER_DAYS = 4          # tunable once the publication time of NSE's file is measured
MIN_BREADTH_STOCKS = 200
PRICE_SOURCE = "NSE end-of-day file"
INDEX_SOURCE = "NSE end-of-day index file"
FLOW_SOURCE = "NSE FII/DII data"
DEAL_SOURCE = "NSE large-deals data"
DEAL_KINDS = ("bulk", "block", "short")
TOP_N = 5                          # rows listed in a market-wide deals answer (2 each when two kinds are shown)
STOCK_DEAL_ROWS = 6                # deals listed for one stock; the count of all matches is stated
SCAN_DAYS = 30                     # stored days searched for one stock
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
    kind: str                     # move | return | events | index | index_return | compare | flows | flows_period | deals | deals_market | clarify
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


def crore_text(x: float, signed: bool = False) -> str:
    """Rs crore as NSE reports FII/DII flows; a sign goes in front of the rupee symbol."""
    sign = ("+" if x > 0 else "-" if x < 0 else "") if signed else ""
    return f"{sign}₹{abs(x):,.2f} crore"


def _flow_problem(rows: dict) -> str:
    """Why a day's FII/DII rows cannot be right, or ''. Both categories must be there and buy - sell must equal net."""
    for cat in ("FII/FPI", "DII"):
        if cat not in rows:
            return f"{cat} is missing"
        _c, buy, sell, net = rows[cat]
        if abs(buy - sell - net) > 0.02:
            return f"{cat} bought minus sold is not the net"
    return ""


def _deal_text(kind: str, row, value=None) -> str:
    """One deal as plain text; the value in Rs crore (quantity x price) is computed here and labelled as such."""
    if kind == "short":
        return f"{row[0]}: {row[1]:,} shares in the short-selling report"
    symbol, client, side, qty, price, remarks = row
    text = f"{symbol}: {side} {qty:,} shares" + (f" at {money(price)}" if price is not None else "")
    if value is not None:
        text += f" (about ₹{value:,.2f} crore, quantity times price)"
    return text + (f", client {client}" if client else "")


def level_text(x: float) -> str:
    """An index level: points, so no currency sign."""
    return f"{x:,.2f}"


def _is_not_price_type(name: str) -> bool:
    """Series whose percentage change means nothing: a volatility level (India VIX) and an index of accumulated dividend
    points (Nifty50 Dividend Points moved +920% in 6 months in the real files)."""
    n = name.lower()
    return "vix" in n or "dividend points" in n


def _index_row_problem(row) -> str:
    """Why the figures of an index row cannot all be right, or '' when they agree. The change in points and in percent
    must fit the close (NSE rounds both, so the allowed gap follows from rounding), and open and close lie in [low, high]."""
    if row.points is not None and row.pct is not None:
        prev = row.close - row.points
        if prev <= 0:
            return "the previous close would not be positive"
        if abs(row.points / prev * 100 - row.pct) > 0.011 + 0.01 / prev * 100:
            return "points and percent change differ"
    if row.low is not None and row.high is not None:
        if row.low > row.high or any(v is not None and not row.low <= v <= row.high for v in (row.open, row.close)):
            return "open or close outside the day's range"
    return ""


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


def _clip(text: str, n: int = 200) -> str:
    """Whitespace-collapsed text cut at a word boundary, with an ellipsis when shortened."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def _unique_announcements(anns):
    """NSE sometimes lists the same filing twice (two PDFs, same time and category)."""
    seen, out = set(), []
    for a in anns:
        key = (a.category, a.published, " ".join(a.text.split())[:80])
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def _meeting_title(group) -> str:
    """NSE lists a board meeting twice (an intimation and a purpose record); prefer the real purpose."""
    results = any(m.results_related for m in group)
    label = "Results board meeting" if results else "Board meeting"
    real = [m for m in group if not m.purpose.lower().startswith("board meeting intimation") and m.purpose]
    return f"{label}: {real[0].purpose[:120]}" if real else f"{label} (intimation)"


def _group_meetings(meetings):
    by = {}
    for m in meetings:
        by.setdefault(m.meeting_date, []).append(m)
    return by


class Assembler:
    def __init__(self, store, nse, news, resolver, now=None, names=None, index=None):
        self.store, self.nse, self.news, self.resolver = store, nse, news, resolver
        self.index = index                                  # IndexClient (NSE end-of-day index file), or None
        self.special_days = special_days_from_env()         # weekday special sessions (SPECIAL_SESSION_DAYS)
        self.names = dict(names or {})                      # {symbol: full company name} from NSE's equity list
        self._now = now or (lambda: datetime.now(IST))

    # ---------------------------------------------------------------- shared helpers
    def _name(self, inst) -> str:
        """The name shown for an instrument: NSE's full company name when stored, else the price-file name."""
        return self.names.get(inst.symbol) or inst.name

    def _stamp(self) -> str:
        return self._now().astimezone(IST).isoformat(timespec="seconds")

    def _today(self) -> str:
        return self._now().astimezone(IST).date().isoformat()

    def _no_close_today(self, day: str) -> bool:
        """True on a weekday after the 09:15 open when the newest stored close is from an earlier day: today's session is
        in progress, or NSE's file is not out, or it is a holiday. Before 09:15 the earlier close is simply the latest one."""
        now = self._now().astimezone(IST)
        return day < now.date().isoformat() and now.weekday() < 5 and now.time() >= time(9, 15)

    def _resolve(self, query):
        res = self.resolver.resolve(query)
        if res.status in ("exact", "confident"):
            inst = res.best
            if inst.kind == "index":
                return None, Packet("clarify", "refused", inst.symbol, self._name(inst), self._stamp(), gaps=(Gap(
                    "index_not_supported", "For an index I can give the closing level on a day (for example 'Nifty 50 close \
today'), not a stock-style move, return or filings list.", "missing"),))
            return inst, None
        return None, Packet("clarify", "clarify", as_of=self._stamp(),
                            gaps=(Gap("symbol_unresolved", format_choices(res, self._name), "missing"),),
                            choices=tuple(f"{self._name(m.instrument)} ({m.instrument.symbol})" for m in res.matches))

    def _refuse(self, kind, inst, *gaps, facts=()):
        return Packet(kind, "refused", inst.symbol, self._name(inst), self._stamp(), facts=tuple(facts), gaps=tuple(gaps))

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
                 Fact("Reference price (NSE's previous-close field)" if move.status == "possible_listing" else
                      "Previous close" + (" (unadjusted)" if move.status == "blocked_by_corporate_action" else ""),
                      money(move.prev_close), move.prev_close, PRICE_SOURCE, None if move.status == "possible_listing" else prior or None)]
        gaps = self._stale_gap(day) if on is None else []
        if move.status not in ("blocked_by_corporate_action", "possible_listing"):
            facts.append(Fact("Change from previous close", pct_text(move.pct), move.pct, PRICE_SOURCE, day))
        if move.status == "possible_listing":
            gaps.append(Gap("possible_listing", f"{sym} is in none of the price files of the previous {LISTING_LOOKBACK} "
                            "trading days, so this may be its first trading day (a new listing) or a return after a long "
                            "gap. NSE's previous-close field is then an issue or reference price, so no change from the "
                            "previous close is given.", "limit"))
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
        odd = [d for d in (prior, day) if d and is_special_session(d, self.special_days)]
        if prior in odd:                                         # its close is unknown: nothing from that calendar day counts
            lo = _at((Date.fromisoformat(prior) + timedelta(days=1)).isoformat(), time(0, 0))
        if day in odd:                                           # its hours are unknown: only items before that day are certain
            hi = _at(day, time(0, 0))
        if odd:
            if len(odd) == 2:
                lead = f"{human_date(odd[0])} and {human_date(odd[1])} were special trading sessions"
                tail = "filings and news published on those days cannot be placed before or after the close and are not listed."
            elif odd[0] == prior:
                lead = f"The previous trading day, {human_date(prior)}, was a special trading session"
                tail = (f"filings and news published on {human_date(prior)} cannot be placed before or after its close and "
                        "are not listed for this move.")
            else:
                lead = f"{human_date(day)} was a special trading session"
                tail = "filings and news published on that day cannot be placed before or after the close and are not listed."
            gaps.append(Gap("special_session", f"{lead} (a weekend or other session with hours of its own), so {tail}", "limit"))

        def timing(t):
            base = "after the previous close, before the open" if t < opening else "during market hours"
            # with a special session in the window the label also names the publication date, so the reader can check it
            return f"published {human_date(t.date().isoformat())}, {base}" if odd else base

        factors, name = [], self._name(inst)
        try:                                                     # filings; the wide window also gives the full name
            anns = _unique_announcements(
                self.nse.announcements(sym, (Date.fromisoformat(day) - timedelta(days=45)).isoformat(), day))
            if anns:
                name = self.names.get(sym) or anns[0].company or name
            for a in anns:
                t = _aware(a.published)
                if lo < t <= hi:
                    factors.append(Factor("announcement", a.published, timing(t), a.category, "NSE announcement",
                                          _clip(a.text), a.url))
        except NseError:
            gaps.append(Gap("announcements_unavailable", "NSE announcements could not be checked.", "missing"))
        for a in self.store.get_actions(sym):
            if (prior is None or a.ex_date > prior) and a.ex_date <= day:
                factors.append(Factor("corporate_action", a.ex_date, "ex-date", a.subject, "NSE corporate actions"))
        try:
            first = prior or _prev_day(day)
            for d, group in sorted(_group_meetings(self.nse.board_meetings(sym, first, day)).items()):
                if first <= d <= day:
                    factors.append(Factor("board_meeting", d, "meeting date", _meeting_title(group),
                                          "NSE board meetings"))
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
        none_found = complete and not factors and not odd and move.status in ("ok", "unverified_large_move", "possible_listing")
        if none_found:
            gaps.append(Gap("no_clear_evidence", f"No filing, corporate action, board meeting or relevant news was "
                            f"found between the {human_date(prior) if prior else 'previous'} close and the "
                            f"{human_date(day)} close.", "info"))
        return Packet("move", "ok", sym, name, self._stamp(), tuple(facts), tuple(context), tuple(factors),
                      tuple(gaps), complete, none_found)

    # ---------------------------------------------------------------- period return
    def period_return(self, query, label=None, start=None, end=None) -> Packet:
        idx = find_index(query)
        if idx is not None:
            return self.index_return(idx, label, start, end)
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
            return Packet("return", "ok", sym, self._name(inst), self._stamp(), tuple(facts), (), (), tuple(gaps), True)
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

    # ---------------------------------------------------------------- FII/DII flows (stored daily snapshots)
    def _flow_record(self, day):
        try:
            return self.store.get_flow(day)
        except AttributeError:
            return None

    def fii_dii(self, on=None, label=None) -> Packet:
        """FII/FPI and DII buying, selling and net (Rs crore) for one stored day, or added up over a period when EVERY trading
        day of the period is stored. NSE gives the latest day only, so history exists only from the day storage began; a
        period with missing days is not totalled, the days that are stored are listed instead."""
        def refuse(*gaps, facts=(), kind="flows"):
            return Packet(kind, "refused", "", "FII/DII", self._stamp(), tuple(facts), gaps=tuple(gaps))
        try:
            days = list(self.store.flow_days())
        except AttributeError:
            days = []
        if not days:
            return refuse(Gap("no_flow_data", "No FII/DII records are stored yet.", "missing"))
        if label:
            return self._fii_dii_period(label, days, refuse)
        day = on
        if on:
            try:
                Date.fromisoformat(on)
            except (TypeError, ValueError):
                return refuse(Gap("bad_date", "I could not read that date.", "missing"))
        else:
            with_fii = [d for d in days if (self._flow_record(d) or {}).get("fii")]
            if not with_fii:
                return refuse(Gap("no_flow_data", "No FII/DII records are stored yet.", "missing"))
            day = with_fii[-1]
        rec = self._flow_record(day) if day in days else None
        with_fii = [d for d in days if (self._flow_record(d) or {}).get("fii")]      # days that hold FII/DII (a day may hold only deals)
        latest_price = self.store.latest_day()
        requested = None
        if on and not (rec and rec.get("fii")) and with_fii and latest_price and on > latest_price and on > with_fii[-1]:
            # A day after the latest priced day, such as today before NSE has published: it may still arrive, so show the
            # latest stored day and say plainly which day that is. A past day that is missing cannot be added and is refused.
            requested, day = on, with_fii[-1]
            rec = self._flow_record(day)
        if not rec or not rec.get("fii"):
            first = with_fii[0] if with_fii else days[0]
            return refuse(Gap("no_flow_record", f"No FII/DII record is stored for {human_date(day)}; records start on "
                              f"{human_date(first)}, and NSE only publishes the latest day, so earlier days cannot be added.",
                              "missing"))
        rows = {r[0]: r for r in rec["fii"]}
        bad = _flow_problem(rows)
        if bad:
            return refuse(Gap("flows_inconsistent", f"NSE's FII/DII figures for {human_date(day)} do not agree with each "
                              f"other ({bad}), so none are given.", "missing"))
        facts = []
        for cat in ("FII/FPI", "DII"):
            _c, buy, sell, net = rows[cat]
            facts += [Fact(f"{cat}: bought", crore_text(buy), buy, FLOW_SOURCE, day),
                      Fact(f"{cat}: sold", crore_text(sell), sell, FLOW_SOURCE, day),
                      Fact(f"{cat}: net", crore_text(net, signed=True), net, FLOW_SOURCE, day)]
        gaps = []
        if requested:
            gaps.append(Gap("flows_not_yet", f"FII/DII data for {human_date(requested)} is not stored yet; this is the "
                            "latest day stored.", "limit"))
        elif on is None and latest_price and day < latest_price:
            gaps.append(Gap("flows_not_yet", f"FII/DII data for {human_date(latest_price)} is not stored yet; this is the "
                            "latest day stored.", "limit"))
        return Packet("flows", "ok", "", "FII/DII", self._stamp(), tuple(facts), (), (), tuple(gaps), True)

    def _fii_dii_period(self, label, days, refuse) -> Packet:
        end = self.store.latest_day()
        target = period_start(end, label) if end else None
        if not end:
            return refuse(Gap("no_price_data", "No price data is stored, so I do not know the trading days.", "missing"),
                          kind="flows_period")
        if not target:
            return refuse(Gap("bad_period", "Unknown period; use 1w, 1m, 3m, 6m, 1y or ytd.", "missing"), kind="flows_period")
        start_day = self.store.day_on_or_before(target)
        if start_day is None or (Date.fromisoformat(target) - Date.fromisoformat(start_day)).days > MAX_GAP_DAYS:
            return refuse(Gap("insufficient_history", f"Stored history starts {self.store.earliest_day()}; need data near "
                              f"{target}.", "missing"), kind="flows_period")
        window = [d for d in self.store.all_days() if target < d <= end]      # the trading days after the start, up to the end
        have = {}
        for d in window:
            rec = self._flow_record(d) if d in days else None
            if rec and rec.get("fii"):
                have[d] = {r[0]: r for r in rec["fii"]}
        for d, rows in have.items():
            bad = _flow_problem(rows)
            if bad:
                return refuse(Gap("flows_inconsistent", f"NSE's FII/DII figures for {human_date(d)} do not agree with each "
                                  f"other ({bad}), so no total is given.", "missing"), kind="flows_period")
        span = f"{human_date(window[0])} to {human_date(window[-1])}" if window else ""
        if not have:
            first_fii = next((d for d in days if (self._flow_record(d) or {}).get("fii")), days[0])
            return refuse(Gap("no_flow_record", f"No FII/DII data is stored for {span or 'this period'}; records start on "
                              f"{human_date(first_fii)}.", "missing"), kind="flows_period")
        if len(have) < len(window):
            facts = []
            for d in sorted(have)[-3:]:
                for cat in ("FII/FPI", "DII"):
                    net = have[d][cat][3]
                    facts.append(Fact(f"{cat}: net, {human_date(d)}", crore_text(net, signed=True), net, FLOW_SOURCE, d))
            return refuse(Gap("flows_partial", f"I have FII/DII data for only {len(have)} of the {len(window)} trading days "
                              f"from {span}, so I am not giving a total.", "missing"), facts=facts, kind="flows_period")
        facts = [Fact("Trading days added up", f"{len(window)} trading days, {span}", len(window), FLOW_SOURCE, window[-1])]
        for cat in ("FII/FPI", "DII"):
            buy = round(sum(have[d][cat][1] for d in window), 2)
            sell = round(sum(have[d][cat][2] for d in window), 2)
            net = round(sum(have[d][cat][3] for d in window), 2)
            facts += [Fact(f"{cat}: bought", crore_text(buy), buy, FLOW_SOURCE, window[-1]),
                      Fact(f"{cat}: sold", crore_text(sell), sell, FLOW_SOURCE, window[-1]),
                      Fact(f"{cat}: net", crore_text(net, signed=True), net, FLOW_SOURCE, window[-1])]
        gaps = [Gap("flows_added_up", "Totals are the daily NSE figures added up here.", "info")]
        return Packet("flows_period", "ok", "", "FII/DII", self._stamp(), tuple(facts), (), (), tuple(gaps), True)

    # ---------------------------------------------------------------- bulk, block and short-selling deals
    def large_deals(self, query=None, kind=None, on=None) -> Packet:
        """Deals for one stock over the stored days, or the market-wide picture of one stored day (counts and the largest
        by value). Only stored, count-checked days are used, so 'none found' is said only for days that have data."""
        kinds = (kind,) if kind in DEAL_KINDS else DEAL_KINDS
        try:
            days = list(self.store.flow_days())
        except AttributeError:
            days = []

        def refuse(inst, *gaps):
            return Packet("deals", "refused", inst.symbol if inst else "", inst.name if inst else "Large deals", self._stamp(),
                          gaps=tuple(gaps))
        inst = None
        if query:
            inst, clar = self._resolve(query)
            if clar is not None:
                return clar
        if not days:
            return refuse(inst, Gap("no_flow_data", "No bulk, block or short-selling records are stored yet.", "missing"))
        if on:
            try:
                Date.fromisoformat(on)
            except (TypeError, ValueError):
                return refuse(inst, Gap("bad_date", "I could not read that date.", "missing"))
            if on not in days:
                return refuse(inst, Gap("no_flow_record", f"No deal records are stored for {human_date(on)}; records start on "
                                        f"{human_date(days[0])}.", "missing"))
        return self._deals_for_stock(inst, kinds, days, on, refuse) if inst else self._deals_market(kinds, days, on, refuse)

    def _deals_for_stock(self, inst, kinds, days, on, refuse) -> Packet:
        scan = [on] if on else days[-SCAN_DAYS:]
        found, searched = [], {}
        for d in reversed(scan):
            rec = self._flow_record(d) or {}
            for k in kinds:
                if k not in rec:
                    continue
                searched.setdefault(d, []).append(k)
                for row in rec[k]:
                    if row[0] == inst.symbol:
                        found.append((d, k, row))
        if not searched:
            return refuse(inst, Gap("no_flow_record", "No stored day has the kind of deal asked about.", "missing"))
        first, last = min(searched), max(searched)
        gaps = []
        if any(len(v) < len(kinds) for v in searched.values()) or len(searched) < len(scan):
            gaps.append(Gap("deals_partial", "Not every stored day has every kind of deal record, so a missing deal is "
                            "only certain for days that have the data.", "limit"))
        if not found:
            facts = (Fact("Days searched", f"{len(searched)} stored day(s), {human_date(first)} to {human_date(last)}",
                          len(searched), DEAL_SOURCE, last),)
            return Packet("deals", "ok", inst.symbol, self._name(inst), self._stamp(), facts, (), (),
                          tuple(gaps + [Gap("no_deals_found", "No matching deals were found.", "info")]), True)
        shown = found[:STOCK_DEAL_ROWS]
        facts = [Fact("Matching records", f"{len(found)} in {len(searched)} stored day(s), {human_date(first)} to "
                      f"{human_date(last)}" + (f"; the latest {len(shown)} are listed" if len(shown) < len(found) else ""),
                      len(found), DEAL_SOURCE, last)]
        for d, k, row in shown:
            facts.append(Fact(f"{k.capitalize()} deal, {human_date(d)}", _deal_text(k, row), row[3] if k != "short" else row[1],
                              DEAL_SOURCE, d))
        return Packet("deals", "ok", inst.symbol, self._name(inst), self._stamp(), tuple(facts), (), (), tuple(gaps), True)

    def _deals_market(self, kinds, days, on, refuse) -> Packet:
        def has(d, k):
            return k in (self._flow_record(d) or {})
        picked = {}
        for k in kinds:
            cand = [on] if on else [d for d in reversed(days) if has(d, k)][:1]
            if cand and has(cand[0], k):
                picked[k] = cand[0]
        if not picked:
            return refuse(None, Gap("no_flow_record", "No stored day has that kind of deal record.", "missing"))
        both = sum(1 for k in picked if k != "short") > 1
        n_top = 2 if both else TOP_N
        counts, ranked = [], []
        for k, d in picked.items():
            rows = (self._flow_record(d) or {})[k]
            counts.append(f"{len(rows)} {k}")
            if k == "short":
                top = sorted(rows, key=lambda r: -r[1])[:n_top]
                ranked += [(k, d, r, None) for r in top] if kinds == ("short",) else []
            else:
                priced = [r for r in rows if r[4] is not None and r[3] is not None]
                top = sorted(priced, key=lambda r: -(r[3] * r[4]))[:n_top]
                ranked += [(k, d, r, round(r[3] * r[4] / 1e7, 2)) for r in top]
        facts = []
        for k, d in picked.items():
            n = len((self._flow_record(d) or {})[k])
            facts.append(Fact(f"{k.capitalize()} records, {human_date(d)}", f"{n}", n, DEAL_SOURCE, d))
        for k, d, r, value in ranked:
            facts.append(Fact(f"Largest {k}, {human_date(d)}", _deal_text(k, r, value), value if value is not None else r[1],
                              DEAL_SOURCE, d))
        gaps = []
        latest_price = self.store.latest_day()
        if on is None and latest_price and any(d < latest_price for k, d in picked.items() if k != "short"):
            gaps.append(Gap("flows_not_yet", f"Deal data for {human_date(latest_price)} is not stored yet; this is the latest "
                            "day stored.", "limit"))
        if len(picked) < len(kinds):
            gaps.append(Gap("deals_partial", "Some kinds of deal record are not stored for any day yet.", "limit"))
        if picked.get("bulk") and picked.get("bulk") == picked.get("block"):
            rec = self._flow_record(picked["bulk"]) or {}
            if {tuple(r) for r in rec.get("bulk", [])} & {tuple(r) for r in rec.get("block", [])}:
                gaps.append(Gap("deals_overlap", "Some trades appear in both the bulk and the block list, so counts and "
                                "rankings across both can include the same trade twice.", "info"))
        return Packet("deals_market", "ok", "", "Large deals", self._stamp(), tuple(facts), (), (), tuple(gaps), True)

    # ---------------------------------------------------------------- compare two returns
    def compare_returns(self, queries, label=None) -> Packet:
        """Two stocks, ETFs or NSE indices over the same period: each side is its own period_return packet (so every refusal
        rule of a single return applies unchanged), both read the same stored trading days, and the difference in percentage
        points is computed here. If either side cannot be given, nothing is ranked."""
        qs = [str(q).strip() for q in list(queries)[:2]]
        if len(qs) != 2 or not all(qs):
            return Packet("compare", "refused", name="", as_of=self._stamp(), gaps=(Gap(
                "compare_needs_two", "I need two names to compare, for example TCS and Infosys.", "missing"),))
        sides = [self.period_return(q, label=label) for q in qs]
        for side in sides:
            if side.status == "clarify":                       # an ambiguous name: ask, do not guess which one was meant
                return side
        names = [side.name if side.kind == "index_return" else (f"{side.name} ({side.symbol})" if side.symbol else side.name)
                 for side in sides]
        title = " vs ".join(names)
        if sides[0].symbol and sides[0].symbol == sides[1].symbol:
            return Packet("compare", "refused", "", title, self._stamp(), gaps=(Gap(
                "compare_same", f"Both names point to {names[0]}; give two different companies or indices.",
                "missing"),))
        bad = [(n, side) for n, side in zip(names, sides) if side.status != "ok"]
        if bad:
            gaps, seen = [], set()
            for n, side in bad:
                for g in side.gaps:
                    gaps.append(Gap(g.code, g.text if g.text.startswith(side.name) else f"{n}: {g.text}", g.level))
            good = [(n, side) for n, side in zip(names, sides) if side.status == "ok"]
            facts = tuple(Fact(f"{n}: {side.facts[2].label}", side.facts[2].value, side.facts[2].raw, side.facts[2].source,
                               side.facts[2].date) for n, side in good)
            return Packet("compare", "refused", "", title, self._stamp(), facts, gaps=tuple(gaps))
        a, b = sides
        if (a.facts[0].date, a.facts[1].date) != (b.facts[0].date, b.facts[1].date):
            return Packet("compare", "refused", "", title, self._stamp(), gaps=(Gap(
                "compare_dates_differ", "The two returns do not cover the same dates, so they are not compared.",
                "missing"),))
        diff = round(a.facts[2].raw - b.facts[2].raw, 2)
        facts = [Fact(f"{n}: {side.facts[2].label}", side.facts[2].value, side.facts[2].raw, side.facts[2].source,
                      side.facts[2].date) for n, side in zip(names, sides)]
        facts.append(Fact("Difference in percentage points", f"{abs(diff):.2f}", diff, a.facts[2].source, a.facts[2].date))
        context = tuple(Fact(f"{n}: {f.label}", f.value, f.raw, f.source, f.date)
                        for n, side in zip(names, sides) for f in side.facts[:2])
        kinds = {side.kind for side in sides}
        basis = ("Both are price returns; dividends are not included." if kinds == {"return"} else
                 "Both are changes in index level; dividends are not included unless the index itself is a total-return "
                 "index." if kinds == {"index_return"} else
                 "Stock figures are price returns and index figures are changes in level; dividends are not included "
                 "unless the index itself is a total-return index.")
        gaps = [Gap("compare_basis", basis, "info")]
        for side in sides:
            for g in side.gaps:
                if g.code not in ("price_return_only", "index_level_change") and all(g.code != x.code for x in gaps):
                    gaps.append(g)
        return Packet("compare", "ok", "", title, self._stamp(), tuple(facts), context, (), tuple(gaps), True)

    # ---------------------------------------------------------------- index return
    def index_return(self, inst, label=None, start=None, end=None) -> Packet:
        """Change in an NSE index's level between two closes: the last stored trading day on or before the target start
        (the same calendar and 7-day limit as stock returns) and the latest stored day. Two index files are read; if either
        is missing, or the index is not in the older one, no return is given. Code-built; no cause is ever stated."""
        def refuse(*gaps, facts=()):
            return Packet("index_return", "refused", inst.symbol, inst.name, self._stamp(), tuple(facts), gaps=tuple(gaps))
        if _is_not_price_type(inst.name):
            return refuse(Gap("index_not_a_price", f"{inst.name} is not a price-type index, so a percentage change over a "
                              "period is not meaningful. I can give its close on a day.", "limit"))
        if self.index is None:
            return refuse(Gap("index_file_unavailable", "NSE's index file could not be fetched here, so no return is "
                              "given.", "missing"))
        end = end or self.store.latest_day()
        if not end:
            return refuse(Gap("no_price_data", "No price data is stored.", "missing"))
        target = start or (period_start(end, label) if label else None)
        if not target:
            return refuse(Gap("bad_period", "Unknown period; use 1w, 1m, 3m, 6m, 1y, ytd or give a start date.", "missing"))
        end_day, start_day = self.store.day_on_or_before(end), self.store.day_on_or_before(target)
        if end_day is None:
            return refuse(Gap("no_price_data", "No price data is stored.", "missing"))
        if start_day is None or (Date.fromisoformat(target) - Date.fromisoformat(start_day)).days > MAX_GAP_DAYS:
            earliest = self.store.earliest_day()
            return refuse(Gap("insufficient_history", f"Stored history starts {earliest}; need data near {target}.",
                              "missing"))
        if start_day == end_day:
            return refuse(Gap("no_data", "Start and end fall on the same trading day.", "missing"))
        rows = {}
        for day in (start_day, end_day):
            try:
                found = find_row(self.index.day(day), inst.name)
            except NseError:
                return refuse(Gap("index_file_unavailable", f"NSE's index file for {human_date(day)} could not be fetched "
                                  "(it may not be published yet), so no return is given.", "missing"))
            if found is None:
                return refuse(Gap("index_not_in_file", f"{inst.name} is not in NSE's index file for {human_date(day)}, so "
                                  "no return is given for this period.", "missing"))
            if found.close is None or found.close <= 0 or _index_row_problem(found):
                return refuse(Gap("index_data_inconsistent", f"NSE's index file figures for {inst.name} on "
                                  f"{human_date(day)} are missing or do not agree with each other, so no return is given.",
                                  "missing"))
            rows[day] = found
        a, b = rows[start_day].close, rows[end_day].close
        pct = round((b / a - 1) * 100, 2)
        src = INDEX_SOURCE
        facts = (Fact(f"Closing level, {human_date(start_day)}", level_text(a), a, src, start_day),
                 Fact(f"Closing level, {human_date(end_day)}", level_text(b), b, src, end_day),
                 Fact("Change in index level", pct_text(pct), pct, src, end_day))
        gaps = [Gap("index_level_change", "This is the change in the index level between the two closes; dividends are "
                    "not included unless the index itself is a total-return index.", "info")]
        gaps += self._stale_gap(end_day) if end == self.store.latest_day() else []
        return Packet("index_return", "ok", inst.symbol, inst.name, self._stamp(), facts, (), (), tuple(gaps), True)

    # ---------------------------------------------------------------- index close
    def _index_refusal(self, inst, *gaps, facts=()):
        return Packet("index", "refused", inst.symbol, inst.name, self._stamp(), facts=tuple(facts), gaps=tuple(gaps))

    def index_close(self, query, on=None) -> Packet:
        """Where an NSE index closed on a day (default: the latest stored trading day). Code-built, from NSE's end-of-day
        index file; no cause is ever stated. A missing or odd file is a gap, never another day's figures."""
        inst = find_index(query)
        if inst is None:
            return Packet("clarify", "clarify", as_of=self._stamp(), gaps=(Gap(
                "index_unresolved", f"I couldn't find an NSE index matching \"{query}\". Try a name such as Nifty 50, "
                "Nifty Bank or India VIX.", "missing"),))
        if self.index is None:
            return self._index_refusal(inst, Gap("index_file_unavailable", "NSE's index file could not be fetched "
                                                 "here, so no level is given.", "missing"))
        latest = self.store.latest_day()
        if not latest:
            return self._index_refusal(inst, Gap("no_price_data", "No price data is stored, so the latest trading "
                                                 "day is unknown.", "missing"))
        if on is None:
            day = latest
        else:
            try:
                Date.fromisoformat(on)
            except (TypeError, ValueError):
                return self._index_refusal(inst, Gap("bad_date", "I could not read that date.", "missing"))
            if on > latest:
                return self._index_refusal(inst, Gap("no_index_data", f"No end-of-day data is stored for {human_date(on)} "
                                                     f"yet; the latest stored day is {human_date(latest)}.", "missing"))
            earliest = self.store.earliest_day() if hasattr(self.store, "earliest_day") else None
            if not self.store.has_day(on):
                if earliest and on < earliest:
                    return self._index_refusal(inst, Gap("no_index_data", f"Index data older than {human_date(earliest)} "
                                                         "is not available.", "missing"))
                return self._index_refusal(inst, Gap("no_session", f"No NSE end-of-day data is stored for {human_date(on)}; "
                                                     "it may have been a weekend or market holiday, so no close is given.",
                                                     "missing"))
            day = on
        latest_asked, fell_back = day, None
        try:
            rows = self.index.day(day)
        except NseError:
            rows = None
            if on is None:                       # the newest file may simply not be out yet: use the day before, and say so
                before = self.store.day_on_or_before((Date.fromisoformat(day) - timedelta(days=1)).isoformat())
                if before and before != day:
                    try:
                        rows = self.index.day(before)
                        fell_back, day = latest_asked, before
                    except NseError:
                        rows = None
            if rows is None:
                return self._index_refusal(inst, Gap("index_file_unavailable", f"NSE's index file for "
                                                     f"{human_date(latest_asked)} could not be fetched (it may not be "
                                                     "published yet), so no level is given.", "missing"))
        row = find_row(rows, inst.name)
        if row is None:
            return self._index_refusal(inst, Gap("index_not_in_file", f"{inst.name} is not in NSE's index file for "
                                                 f"{human_date(day)}.", "missing"))
        if row.close is None or row.close <= 0:
            return self._index_refusal(inst, Gap("index_figures_incomplete", f"NSE's index file has no closing level for "
                                                 f"{inst.name} on {human_date(day)}.", "missing"))
        problem = _index_row_problem(row)
        if problem:
            return self._index_refusal(inst, Gap("index_data_inconsistent", f"NSE's index file figures for {inst.name} on "
                                                 f"{human_date(day)} do not agree with each other ({problem}), so no level "
                                                 "is given.", "missing"))
        src = INDEX_SOURCE
        facts = [Fact(f"Closing level, {human_date(day)}", level_text(row.close), row.close, src, day)]
        gaps = self._stale_gap(day) if on is None else []
        if fell_back:
            gaps.append(Gap("index_latest_missing", f"NSE's index file for {human_date(fell_back)} is not available yet, so "
                            f"this is the previous trading day, {human_date(day)}.", "limit"))
        elif on is None and self._no_close_today(day):
            gaps.append(Gap("no_close_today", "No close for today is stored yet (the market may still be open, closed for a "
                            f"holiday, or NSE's file may not be out yet), so this is the last stored close, {human_date(day)}.",
                            "limit"))
        if row.points is not None and row.pct is not None:
            facts.append(Fact("Points change", f"{row.points:+,.2f}", row.points, src, day))
            facts.append(Fact("Change from previous close", pct_text(row.pct), row.pct, src, day))
        else:
            gaps.append(Gap("index_change_missing", "NSE's file gives no change figure for this index on this date.",
                            "limit"))
        for label, value in (("Open", row.open), ("High", row.high), ("Low", row.low)):
            if value is not None:
                facts.append(Fact(label, level_text(value), value, src, day))
        for label, value, text in (("P/E", row.pe, None), ("P/B", row.pb, None), ("Dividend yield", row.dy, "%")):
            if value is not None:
                facts.append(Fact(label, f"{value:.2f}{text or ''}", value, src, day))
        return Packet("index", "ok", inst.symbol, inst.name, self._stamp(), tuple(facts), (), (), tuple(gaps), True)

    # ---------------------------------------------------------------- recent events and what is due
    def events(self, query, days=14) -> Packet:
        inst, clar = self._resolve(query)
        if clar:
            return clar
        sym, today = inst.symbol, self._today()
        factors, facts, gaps = [], [], []
        start = (Date.fromisoformat(today) - timedelta(days=days)).isoformat()
        name = self._name(inst)
        try:
            anns = _unique_announcements(self.nse.announcements(sym, start, today))
            if anns:
                name = self.names.get(sym) or anns[0].company or name
            for a in anns[:8]:
                factors.append(Factor("announcement", a.published, f"on {human_date(a.published)}", a.category,
                                      "NSE announcement", _clip(a.text), a.url))
        except NseError:
            gaps.append(Gap("announcements_unavailable", "NSE announcements could not be checked.", "missing"))
        try:
            ahead = (Date.fromisoformat(today) + timedelta(days=60)).isoformat()
            for d, group in sorted(_group_meetings(self.nse.board_meetings(sym, start, ahead)).items()):
                factors.append(Factor("board_meeting", d, f"on {human_date(d)}", _meeting_title(group),
                                      "NSE board meetings"))
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
        return Packet("events", "ok", sym, name, self._stamp(), tuple(facts), (), tuple(factors), tuple(gaps),
                      complete, complete and not factors and not facts)
