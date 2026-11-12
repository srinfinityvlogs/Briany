import json
from datetime import datetime

import pytest

from briany.evidence import IST, Assembler
from briany.marketdata import PriceRow
from briany.news import NewsItem, NewsResult
from briany.nse import NseError
from briany.nse_parsers import Announcement, BoardMeeting, CorpAction, EventItem, FiiDii, parse_detail
from briany.store import MemoryStore
from briany.symbols import Instrument, Resolver

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=IST)               # Tuesday morning; latest stored close is 9 Nov
META = {"updated": "2026-11-09", "start": "2025-10-06", "end": "2027-01-09", "count": 1, "blocking": 0, "symbols": 1}
RESOLVER = Resolver([Instrument("ACME", "ACME INDUSTRIES LTD"), Instrument("TATASTEEL", "TATA STEEL LIMITED"),
                     Instrument("TATAPOWER", "TATA POWER CO LTD")])


def act(ex, kind, subject, blocking, symbol="ACME"):
    return CorpAction(symbol, "Acme Industries Limited", ex, None, subject, kind, blocking,
                      detail=parse_detail(kind, subject))


def build_store(acme=None, actions=(), meta=META, fillers=250):
    """acme: {day: (close, prev_close)}. Filler stocks give the market-breadth context."""
    acme = acme or {"2026-11-05": (100.0, 99.0), "2026-11-06": (101.0, 100.0), "2026-11-09": (110.0, 101.0)}
    s = MemoryStore()
    for day, (close, prev) in acme.items():
        rows = {"ACME": PriceRow(close, prev, 1000)}
        for i in range(fillers):                                    # changes of -0.5, 0, +0.5, +1.0, +1.5 percent
            rows[f"FILL{i:03d}"] = PriceRow(100 + ((i % 5) - 1) * 0.5, 100.0, 10)
        s.put_day(day, rows)
    if meta is not None:
        s.set_actions(list(actions), meta)
    return s


class FakeNse:
    def __init__(self, anns=(), meetings=(), flows=(), next_event=None, fail=()):
        self.anns, self.meetings, self.flows, self.next_event, self.fail = list(anns), list(meetings), list(flows), next_event, set(fail)
        self.calls = []

    def _check(self, what):
        self.calls.append(what)
        if what in self.fail:
            raise NseError(f"HTTP 500 ({what})")

    def announcements(self, symbol, start, end):
        self._check("announcements")
        return list(self.anns)

    def board_meetings(self, symbol, start, end):
        self._check("board_meetings")
        return list(self.meetings)

    def fii_dii(self):
        self._check("fii_dii")
        return list(self.flows)

    def next_results(self, symbol, today):
        self._check("next_results")
        return self.next_event


class FakeNews:
    def __init__(self, items=(), status="ok"):
        self.items, self.status, self.calls = list(items), status, []

    def fetch(self, name, symbol, now=None, days=7, limit=5, unique_first=False):
        self.calls.append({"name": name, "symbol": symbol, "unique_first": unique_first})
        return NewsResult(self.status if self.status == "error" else ("ok" if self.items else "none"),
                          "q", tuple(self.items), "", len(self.items))


def ann(published, category, text="details"):
    return Announcement("ACME", "Acme Industries Limited", published, category, text, "https://nsearchives.nseindia.com/x.pdf")


def assembler(store=None, nse=None, news=None):
    return Assembler(store or build_store(), nse or FakeNse(), news or FakeNews(), RESOLVER, now=lambda: NOW)


# ================================================================ why did it move
FULL_NSE = lambda: FakeNse(                                                          # noqa: E731
    anns=[ann("2026-11-06T19:10:58", "Analyst Meet"), ann("2026-11-09T10:15:00", "Outcome of Board Meeting"),
          ann("2026-11-09T16:00:00", "After the close"), ann("2026-11-06T12:00:00", "Before the window")],
    meetings=[BoardMeeting("ACME", "2026-11-09", "Financial Results", "d", "2026-11-02T10:00:00", True),
              BoardMeeting("ACME", "2026-11-09", "Board Meeting Intimation", "d", None, False)],
    flows=[FiiDii("2026-11-09", "DII", 5000.0, 3799.5, 1200.5), FiiDii("2026-11-09", "FII/FPI", 4000.0, 4800.25, -800.25)])
FULL_NEWS = lambda: FakeNews([                                                       # noqa: E731
    NewsItem("Acme wins order", "Reuters", "2026-11-09T08:45:00+05:30", "https://news/1"),
    NewsItem("Acme after the bell", "Mint", "2026-11-09T18:00:00+05:30", "https://news/2"),
    NewsItem("Acme last week", "Mint", "2026-11-05T10:00:00+05:30", "https://news/3")])


def test_move_packet_has_facts_context_and_only_in_window_factors():
    store = build_store(actions=[act("2026-11-09", "dividend", "Dividend - Rs 5 Per Share", False)])
    nse, news = FULL_NSE(), FULL_NEWS()
    p = assembler(store, nse, news).why_moved("acme")
    assert (p.kind, p.status, p.symbol, p.name) == ("move", "ok", "ACME", "Acme Industries Limited")
    assert [(f.label, f.value) for f in p.facts] == [("Official close, 9 Nov 2026", "₹110.00"),
                                                     ("Previous close", "₹101.00"),
                                                     ("Change from previous close", "+8.91%")]
    assert p.facts[2].raw == pytest.approx(8.91, abs=0.005) and p.facts[0].source == "NSE end-of-day file"
    ctx = {f.label: f.value for f in p.context}
    assert ctx["Median change across 251 listed stocks"] == "+0.50%" and ctx["Stocks up / down"] == "151 / 50"
    assert ctx["DII net buying/selling (Rs crore)"] == "+1,200.50" and ctx["FII/FPI net buying/selling (Rs crore)"] == "-800.25"
    assert [(f.kind, f.title) for f in p.factors] == [
        ("announcement", "Analyst Meet"), ("corporate_action", "Dividend - Rs 5 Per Share"),
        ("board_meeting", "Results board meeting: Financial Results"), ("news", "Acme wins order"),
        ("announcement", "Outcome of Board Meeting")]
    timing = {f.title: f.timing for f in p.factors}
    assert timing["Analyst Meet"] == "after the previous close, before the open"
    assert timing["Outcome of Board Meeting"] == "during market hours"
    assert timing["Acme wins order"] == "after the previous close, before the open"
    assert {"After the close", "Before the window", "Acme after the bell", "Acme last week"}.isdisjoint(
        {f.title for f in p.factors})
    assert p.gaps == () and p.evidence_complete and not p.no_clear_evidence
    assert news.calls == [{"name": "Acme Industries Limited", "symbol": "ACME", "unique_first": True}]
    json.dumps(p.to_dict(), ensure_ascii=False)                                     # serialisable


def test_no_clear_evidence_only_when_every_source_was_checked():
    p = assembler().why_moved("acme")
    assert p.factors == () and p.evidence_complete and p.no_clear_evidence
    assert [g.code for g in p.gaps] == ["no_clear_evidence"] and p.gaps[0].level == "info"
    assert "6 Nov 2026 close and the 9 Nov 2026 close" in p.gaps[0].text


@pytest.mark.parametrize("nse_fail,news_status,code", [
    (("announcements",), "ok", "announcements_unavailable"),
    (("board_meetings",), "ok", "board_meetings_unavailable"),
    ((), "error", "news_unavailable")])
def test_a_failed_source_is_a_gap_not_an_absence(nse_fail, news_status, code):
    p = assembler(nse=FakeNse(fail=nse_fail), news=FakeNews(status=news_status)).why_moved("acme")
    assert code in [g.code for g in p.gaps] and not p.evidence_complete and not p.no_clear_evidence
    assert "no_clear_evidence" not in [g.code for g in p.gaps]


def test_missing_flows_are_noted_but_do_not_block_the_answer():
    p = assembler(nse=FakeNse(fail=("fii_dii",))).why_moved("acme")
    assert "flows_unavailable" in [g.code for g in p.gaps] and p.status == "ok" and p.evidence_complete


def test_missing_or_stale_corporate_actions_refuse_the_number():
    p = assembler(build_store(meta=None)).why_moved("acme")
    assert p.status == "refused" and p.facts == () and p.gaps[0].code == "actions_unavailable"
    stale = assembler(build_store(meta={**META, "updated": "2026-11-01"})).why_moved("acme")
    assert stale.status == "refused" and "2026-11-01" in stale.gaps[0].text


def test_ex_date_of_a_bonus_withholds_the_percentage():
    store = build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (50.5, 101.0)},
                        actions=[act("2026-11-09", "bonus", "Bonus 1:1", True)])
    p = assembler(store).why_moved("acme")
    assert p.status == "ok" and [f.label for f in p.facts] == ["Official close, 9 Nov 2026", "Previous close (unadjusted)"]
    assert "corporate_action_on_date" in [g.code for g in p.gaps]
    assert ("corporate_action", "Bonus 1:1") in [(f.kind, f.title) for f in p.factors]


def test_unlisted_large_move_keeps_the_number_but_flags_it():
    store = build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (10.0, 101.0)})
    p = assembler(store).why_moved("acme")
    assert p.facts[2].value == "-90.10%" and "unverified_large_move" in [g.code for g in p.gaps]


def test_old_dates_are_outside_the_news_search():
    store = build_store({"2026-10-19": (90.0, 89.0), "2026-10-20": (91.0, 90.0), "2026-11-09": (110.0, 91.0)})
    news = FakeNews()
    p = assembler(store, news=news).why_moved("acme", on="2026-10-20")
    assert "news_not_covered" in [g.code for g in p.gaps] and not p.evidence_complete and news.calls == []
    assert p.facts[0].label == "Official close, 20 Oct 2026" and "no_clear_evidence" not in [g.code for g in p.gaps]


def test_ambiguous_and_unsupported_queries_never_reach_the_sources():
    nse = FakeNse()
    p = assembler(nse=nse).why_moved("tata")
    assert p.status == "clarify" and len(p.choices) == 2 and p.gaps[0].code == "symbol_unresolved" and nse.calls == []
    idx = Assembler(build_store(), nse, FakeNews(), Resolver([Instrument("ACME", "ACME INDUSTRIES LTD")]),
                    now=lambda: NOW).why_moved("nifty")
    assert idx.status == "refused" and idx.gaps[0].code == "index_not_supported" and nse.calls == []


def test_stale_data_is_flagged():
    store = build_store({"2026-11-03": (100.0, 99.0), "2026-11-04": (101.0, 100.0)})
    p = assembler(store).why_moved("acme")
    assert "data_may_be_stale" in [g.code for g in p.gaps]


# ================================================================ period returns
def returns_store(actions=(), jumps=()):
    s = build_store({"2026-10-09": (90.0, 89.0), "2026-11-09": (110.0, 109.0)}, actions=actions, fillers=0)
    s.add_jumps(list(jumps))
    return s


def test_period_return_ok_lists_dividends_and_the_price_only_caveat():
    p = assembler(returns_store([act("2026-10-20", "dividend", "Dividend - Rs 5 Per Share", False)])).period_return("acme", "1m")
    assert p.status == "ok"
    assert [(f.label, f.value) for f in p.facts] == [
        ("Official close, 9 Oct 2026", "₹90.00"), ("Official close, 9 Nov 2026", "₹110.00"),
        ("Price return", "+22.22%"), ("Dividend, ex-date 20 Oct 2026", "₹5.00")]
    assert [(g.code, g.level) for g in p.gaps] == [("price_return_only", "info")] and p.evidence_complete


def test_period_return_refuses_with_the_reason_as_facts():
    bonus = assembler(returns_store([act("2026-10-20", "bonus", "Bonus 1:1", True)])).period_return("acme", "1m")
    assert bonus.status == "refused" and bonus.gaps[0].code == "corporate_action_in_period"
    assert [(f.label, f.value, f.date) for f in bonus.facts] == [("Corporate action in this period", "Bonus 1:1", "2026-10-20")]
    jump = assembler(returns_store(jumps=[("ACME", "2026-10-25", -90.0)])).period_return("acme", "1m")
    assert jump.status == "refused" and jump.gaps[0].code == "unverified_large_move"
    assert [f.value for f in jump.facts] == ["-90.00%"]


def test_period_return_other_refusals():
    s = returns_store()
    assert assembler(s).period_return("acme", "1y").gaps[0].code == "insufficient_history"
    assert assembler(s).period_return("acme", "weird").gaps[0].code == "bad_period"
    assert assembler(returns_store()).period_return("acme", start="2026-10-09", end="2026-11-09").status == "ok"
    assert assembler(build_store(meta=None)).period_return("acme", "1m").gaps[0].code == "actions_unavailable"
    assert assembler().period_return("tata", "1m").status == "clarify"


# ================================================================ recent events and what is due
def test_events_packet_lists_recent_and_upcoming_items_with_next_results_date():
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet"), ann("2026-11-03T10:00:00", "Press Release")],
                  meetings=[BoardMeeting("ACME", "2026-11-20", "Financial Results", "d", None, True)],
                  next_event=EventItem("ACME", "2026-11-20", "Financial Results", "d"))
    store = build_store(actions=[act("2026-11-25", "dividend", "Dividend - Rs 6 Per Share", False),
                                 act("2026-10-01", "dividend", "Dividend - Rs 2 Per Share", False)])
    p = assembler(store, nse).events("acme")
    assert p.kind == "events" and [(f.label, f.value) for f in p.facts] == [("Next results meeting", "20 Nov 2026")]
    assert [f.kind for f in p.factors] == ["corporate_action", "board_meeting", "announcement", "announcement"]
    assert p.factors[0].timing == "upcoming, ex-date 25 Nov 2026" and p.evidence_complete and p.gaps == ()
    assert "Dividend - Rs 2 Per Share" not in [f.title for f in p.factors]              # past ex-date is not 'due'


def test_events_without_a_scheduled_results_meeting_says_not_announced_not_none_planned():
    p = assembler().events("acme")
    assert [g.code for g in p.gaps] == ["next_results_not_announced"] and p.gaps[0].level == "info"
    assert "does not mean none is planned" in p.gaps[0].text
    failed = assembler(nse=FakeNse(fail=("next_results",))).events("acme")
    assert "calendar_unavailable" in [g.code for g in failed.gaps] and not failed.evidence_complete


# ================================================================ fixes found in the first real packets
def test_duplicate_filings_collapse_and_long_text_is_cut_at_a_word():
    long = "word " * 80
    nse = FakeNse(anns=[ann("2026-11-09T10:15:00", "Outcome of Board Meeting", long),
                        ann("2026-11-09T10:15:00", "Outcome of Board Meeting", long)])       # same filing, two PDFs
    p = assembler(nse=nse, news=FakeNews()).why_moved("acme")
    anns = [f for f in p.factors if f.kind == "announcement"]
    assert len(anns) == 1 and anns[0].detail.endswith("word…") and len(anns[0].detail) <= 201
    short = assembler(nse=FakeNse(anns=[ann("2026-11-09T10:15:00", "X", "  spaced   out  text ")])).why_moved("acme")
    assert short.factors[0].detail == "spaced out text"


def test_board_meeting_title_prefers_the_real_purpose_in_either_order():
    real = BoardMeeting("ACME", "2026-11-09", "Financial Results", "d", None, True)
    intimation = BoardMeeting("ACME", "2026-11-09", "Board Meeting Intimation", "d", None, True)
    for order in ([real, intimation], [intimation, real]):
        p = assembler(nse=FakeNse(meetings=order)).why_moved("acme")
        titles = [f.title for f in p.factors if f.kind == "board_meeting"]
        assert titles == ["Results board meeting: Financial Results"]
    only = assembler(nse=FakeNse(meetings=[intimation])).why_moved("acme")
    assert [f.title for f in only.factors if f.kind == "board_meeting"] == ["Results board meeting (intimation)"]
    plain = BoardMeeting("ACME", "2026-11-09", "Fund Raising", "d", None, False)
    assert [f.title for f in assembler(nse=FakeNse(meetings=[plain])).why_moved("acme").factors
            if f.kind == "board_meeting"] == ["Board meeting: Fund Raising"]


def test_events_uses_the_same_full_company_name_as_the_move_packet():
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet")])
    moved = assembler(nse=nse).why_moved("acme")
    events = assembler(nse=nse).events("acme")
    assert moved.name == events.name == "Acme Industries Limited"
    assert assembler(nse=FakeNse()).events("acme").name == "ACME INDUSTRIES LTD"        # no filings: resolver name
