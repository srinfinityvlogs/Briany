# SPDX-License-Identifier: Apache-2.0
"""Special sessions: a weekend session (Muhurat on Sunday 8 Nov 2026) has hours no source here gives, so items published on that
calendar day are not placed before or after its close and not listed; the answer names the day. Nothing is guessed."""
from datetime import datetime

import pytest

from briany.answer import fallback_text, validate_text
from briany.evidence import IST, Assembler
from briany.nse_parsers import Announcement
from briany.news import NewsItem
from briany.sessions import is_special_session, special_days_from_env
from test_evidence import RESOLVER, FakeNews, FakeNse, build_store

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=IST)
# Fri 6 Nov (normal), Sun 8 Nov (Muhurat, special), Mon 9 Nov (normal)
DAYS = {"2026-11-05": (99.0, 98.0), "2026-11-06": (100.0, 99.0), "2026-11-08": (103.0, 100.0), "2026-11-09": (104.0, 103.0)}


def ann(published, text):
    return Announcement("ACME", "Acme Industries Limited", published, "General", text, "https://nsearchives.nseindia.com/x.pdf")


ANNS = [ann("2026-11-06T19:10:00", "Fri evening, after the Friday close"), ann("2026-11-07T11:00:00", "Saturday morning"),
        ann("2026-11-08T18:30:00", "Sunday during the evening session"), ann("2026-11-09T08:00:00", "Monday before the open"),
        ann("2026-11-09T11:00:00", "Monday during market hours")]


def asm(days=DAYS, anns=ANNS, extra=None, news=()):
    a = Assembler(build_store(acme=days), FakeNse(anns=list(anns)), FakeNews(list(news)), RESOLVER, now=lambda: NOW)
    if extra is not None:
        a.special_days = frozenset(extra)
    return a


# ================================================================ the rule
def test_weekends_are_special_weekdays_only_when_listed():
    assert is_special_session("2026-11-08") and is_special_session("2026-11-07")           # Sunday, Saturday
    assert not is_special_session("2026-11-09") and not is_special_session("2026-11-06")   # Monday, Friday
    assert is_special_session("2026-11-12", frozenset({"2026-11-12"}))                      # a listed weekday
    assert not is_special_session("garbage") and not is_special_session(None)


def test_the_environment_list_ignores_typos_instead_of_stopping_the_bot():
    assert special_days_from_env("2026-11-12, 2027-02-01;bad,2026-13-01 ,") == frozenset({"2026-11-12", "2027-02-01"})
    assert special_days_from_env("") == frozenset()


def test_the_environment_variable_is_read(monkeypatch):
    monkeypatch.setenv("SPECIAL_SESSION_DAYS", "2026-11-06")
    assert Assembler(build_store(), FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW).special_days == frozenset({"2026-11-06"})
    monkeypatch.delenv("SPECIAL_SESSION_DAYS")
    assert Assembler(build_store(), FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW).special_days == frozenset()


# ================================================================ a move ON the special session
def test_a_move_on_a_sunday_session_uses_only_items_before_that_day():
    p = asm().why_moved("acme", on="2026-11-08")
    assert [f.detail for f in p.factors] == ["Fri evening, after the Friday close", "Saturday morning"]
    assert [f.timing for f in p.factors] == ["published 6 Nov 2026, after the previous close, before the open",
                                             "published 7 Nov 2026, after the previous close, before the open"]
    assert "Sunday during the evening session" not in str([f for f in p.factors])         # ambiguous: left out, not mislabelled
    assert [g.code for g in p.gaps if g.code == "special_session"] == ["special_session"]
    assert "8 Nov 2026 was a special trading session" in next(g.text for g in p.gaps if g.code == "special_session")
    assert not p.no_clear_evidence


def test_with_nothing_else_the_answer_does_not_claim_there_was_no_evidence():
    p = asm(anns=[ann("2026-11-08T18:30:00", "Only during the Sunday session")]).why_moved("acme", on="2026-11-08")
    assert p.factors == () and not p.no_clear_evidence and "no_clear_evidence" not in [g.code for g in p.gaps]
    text = fallback_text(p)
    assert "special trading session" in text and "No filing, corporate action" not in text
    assert validate_text(text, p) == []


# ================================================================ a move on the day AFTER the special session
def test_a_monday_after_a_sunday_session_does_not_inherit_the_sunday_evening_items():
    p = asm().why_moved("acme", on="2026-11-09")
    assert [f.timing for f in p.factors] == ["published 9 Nov 2026, after the previous close, before the open",
                                             "published 9 Nov 2026, during market hours"]
    shown = " ".join(str(f) for f in p.factors)
    assert "Monday before the open" in shown and "Monday during market hours" in shown
    assert "Sunday during the evening session" not in shown and "Fri evening" not in shown
    assert next(g.text for g in p.gaps if g.code == "special_session").startswith(
        "The previous trading day, 8 Nov 2026, was a special trading session")
    assert next(g.text for g in p.gaps if g.code == "special_session").endswith("are not listed for this move.")
    assert fallback_text(p).count("special trading session") == 1 and validate_text(fallback_text(p), p) == []


# ================================================================ normal days are untouched
def test_ordinary_weekdays_have_no_special_gap_and_keep_their_window():
    days = {"2026-11-05": (99.0, 98.0), "2026-11-06": (100.0, 99.0), "2026-11-09": (104.0, 100.0)}     # no Sunday stored
    p = asm(days=days).why_moved("acme", on="2026-11-09")
    assert all(g.code != "special_session" for g in p.gaps)
    kept = " ".join(str(f) for f in p.factors)
    assert "Fri evening, after the Friday close" in kept and "Saturday morning" in kept and "Monday during market hours" in kept
    assert "Sunday during the evening session" in kept       # with no stored Sunday session the Sunday item stays in the window


def test_a_listed_weekday_is_treated_like_a_weekend_session():
    days = {"2026-11-05": (99.0, 98.0), "2026-11-06": (100.0, 99.0), "2026-11-09": (104.0, 100.0)}
    anns = [ann("2026-11-05T19:00:00", "Thu evening"), ann("2026-11-06T12:00:00", "Fri noon")]
    p = asm(days=days, anns=anns, extra={"2026-11-06"}).why_moved("acme", on="2026-11-06")
    assert [g.code for g in p.gaps].count("special_session") == 1
    assert "Thu evening" in " ".join(str(f) for f in p.factors) and "Fri noon" not in " ".join(str(f) for f in p.factors)


def test_both_the_move_day_and_the_previous_day_special_gives_one_gap_naming_both():
    days = {"2026-11-06": (100.0, 99.0), "2026-11-07": (101.0, 100.0), "2026-11-08": (103.0, 101.0)}      # Fri, Sat, Sun sessions
    anns = [ann("2026-11-06T19:00:00", "Fri evening"), ann("2026-11-07T10:00:00", "Sat morning"),
            ann("2026-11-07T23:00:00", "Sat night"), ann("2026-11-08T09:00:00", "Sun morning")]
    p = asm(days=days, anns=anns).why_moved("acme", on="2026-11-08")
    gap = next(g for g in p.gaps if g.code == "special_session")
    assert gap.text.startswith("7 Nov 2026 and 8 Nov 2026 were special trading sessions")
    assert [f for f in p.factors] == []                      # Sat 00:00 to Sun 00:00 is Saturday's own ambiguous day
    assert "filings and news published on those days" in gap.text
    assert [g.code for g in p.gaps].count("special_session") == 1


def test_the_caveat_cannot_be_dropped_from_a_message():
    p = asm().why_moved("acme", on="2026-11-09")
    text = fallback_text(p)
    no_caveat = "\n".join(l for l in text.splitlines() if "special trading session" not in l)
    assert validate_text(no_caveat, p) == ["required caveat missing (special_session): include 'special trading session'"]


def test_the_line_a_user_reads_shows_when_each_filing_was_published():
    text = fallback_text(asm().why_moved("acme", on="2026-11-08"))
    assert "NSE filing, published 6 Nov 2026, after the previous close, before the open: General - Fri evening" in text
    assert "published 7 Nov 2026, after the previous close, before the open: General - Saturday morning" in text
    assert "Sunday during the evening session" not in text
    normal = {"2026-11-05": (99.0, 98.0), "2026-11-06": (100.0, 99.0), "2026-11-09": (104.0, 100.0)}
    plain = fallback_text(asm(days=normal).why_moved("acme", on="2026-11-09"))
    assert "published " not in plain.split("Sources")[0] and "NSE filing, after the previous close, before the open" in plain
