import json

import pytest

from briany import answer as A
from briany.evidence import Fact, Factor, Gap, Packet
from briany.marketdata import PriceRow
from briany.nse_parsers import BoardMeeting, EventItem
from test_evidence import (FULL_NEWS, FULL_NSE, NOW, FakeNews, FakeNse, act, ann, assembler, build_store)

DIVIDEND = [act("2026-11-09", "dividend", "Dividend - Rs 5 Per Share", False)]


def packets():
    full = assembler(build_store(actions=DIVIDEND), FULL_NSE(), FULL_NEWS()).why_moved("acme")
    none = assembler().why_moved("acme")
    bonus = assembler(build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (50.5, 101.0)},
                                  actions=[act("2026-11-09", "bonus", "Bonus 1:1", True)])).why_moved("acme")
    big = assembler(build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (10.0, 101.0)})).why_moved("acme")
    stale = assembler(build_store({"2026-11-03": (100.0, 99.0), "2026-11-04": (97.0, 100.0)})).why_moved("acme")
    failed = assembler(nse=FakeNse(fail=("announcements",)), news=FakeNews(status="error")).why_moved("acme")
    refused = assembler(build_store(meta=None)).why_moved("acme")
    clarify = assembler().why_moved("tata")
    s = build_store({"2026-10-09": (90.0, 89.0), "2026-11-09": (110.0, 109.0)}, actions=DIVIDEND[:0] + [
        act("2026-10-20", "dividend", "Dividend - Rs 5 Per Share", False)], fillers=0)
    ret = assembler(s).period_return("acme", "1m")
    ret_block = assembler(build_store({"2026-10-09": (90.0, 89.0), "2026-11-09": (110.0, 109.0)},
                                      actions=[act("2026-10-20", "bonus", "Bonus 1:1", True)], fillers=0)
                          ).period_return("acme", "1m")
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet"), ann("2026-11-03T10:00:00", "Press Release")],
                  meetings=[BoardMeeting("ACME", "2026-11-20", "Financial Results", "d", None, True)],
                  next_event=EventItem("ACME", "2026-11-20", "Financial Results", "d"))
    events = assembler(build_store(actions=[act("2026-11-25", "dividend", "Dividend - Rs 6 Per Share", False)]), nse).events("acme")
    events_empty = assembler().events("acme")
    return {"full": full, "none": none, "bonus": bonus, "big": big, "stale": stale, "failed": failed,
            "refused": refused, "clarify": clarify, "return": ret, "return_block": ret_block, "events": events,
            "events_empty": events_empty}


PK = packets()
GOOD = ("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from the previous close of ₹101.00.\n"
        "The median listed stock moved +0.50% that day, with 151 stocks up and 50 down.\n"
        "Possible contributing factors (not confirmed causes): an NSE filing about an analyst meet after the previous "
        "close, a dividend ex-date, a results board meeting and news from Reuters before the open.")


# ================================================================ the code-built text is always acceptable
@pytest.mark.parametrize("name", sorted(PK))
def test_fallback_text_passes_the_validator_and_fits_a_phone(name):
    p = PK[name]
    text = A.fallback_text(p)
    assert A.validate_text(text, p) == [], text
    assert text and len([l for l in text.splitlines() if l.strip()]) <= A.MAX_LINES


def test_fallback_content_for_the_main_cases():
    full = A.fallback_text(PK["full"])
    assert "Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026." in full
    assert "+8.91% from the previous close of ₹101.00" in full and "Possible contributing factors (not confirmed causes):" in full
    assert "NSE filing, during market hours: Outcome of Board Meeting" in full and "News (Reuters)" in full
    assert "Corporate action, ex-date: Dividend - Rs 5 Per Share" in full and "Board meeting, meeting date" in full
    assert "(plus 1 more not shown)" in full                       # the earlier filing was left out
    assert A.fallback_text(PK["none"]).count("No clear evidence found.") == 1
    bonus = A.fallback_text(PK["bonus"])
    assert "no percentage change is shown" in bonus and "%" not in bonus.split("\n")[1] and "corporate action" in bonus.lower()
    assert "unlisted split" in A.fallback_text(PK["big"])
    refused = A.fallback_text(PK["refused"])
    assert refused.startswith("I can't give a reliable figure") and "could not be verified" in refused
    assert A.fallback_text(PK["clarify"]).startswith("Did you mean")
    ret = A.fallback_text(PK["return"])
    assert "price return from 9 Oct 2026 to 9 Nov 2026: +22.22%" in ret and "excludes dividends" in ret and "₹5.00" in ret
    assert "Bonus 1:1" in A.fallback_text(PK["return_block"])
    ev = A.fallback_text(PK["events"])
    assert "Next results meeting: 20 Nov 2026." in ev and "upcoming, ex-date 25 Nov 2026" in ev
    assert "no upcoming results meeting" in A.fallback_text(PK["events_empty"])


def test_every_required_caveat_phrase_is_present_in_the_fallback():
    for name, p in PK.items():
        low = A.fallback_text(p).lower()
        for g in p.gaps:
            if g.code in A.REQUIRED_PHRASES and p.status == "ok":
                assert A.REQUIRED_PHRASES[g.code] in low, (name, g.code)


# ================================================================ validator accepts good text, in different formats
def test_a_good_model_text_is_accepted_in_any_number_and_date_format():
    p = PK["full"]
    assert A.validate_text(GOOD, p) == []
    reformatted = GOOD.replace("₹110.00", "₹110").replace("₹101.00", "Rs 101.0").replace("9 Nov 2026", "9 November 2026")
    assert A.validate_text(reformatted, p) == []
    assert A.validate_text(GOOD.replace("9 Nov 2026", "Nov 9"), p) == []
    assert A.validate_text(GOOD.replace("9 Nov 2026", "2026-11-09"), p) == []


@pytest.mark.parametrize("change,fragment", [
    (("up 8.91%", "up 9.5%"), "number not in the data: 9.5%"),
    (("₹110.00", "₹112.00"), "number not in the data: ₹112.00"),
    (("9 Nov 2026", "10 Nov 2026"), "date not in the data: 10 Nov 2026"),
    (("9 Nov 2026", "9 Nov 2025"), "date not in the data: 9 Nov 2025"),
    (("before the open", "before the open, around 7:10 PM"), "clock time"),
    (("a dividend ex-date", "a dividend ex-date, because investors liked it"), "causal wording"),
    (("results board meeting", "results board meeting, driven by strong demand"), "causal wording"),
    (("Possible contributing factors", "Possible contributing factors, so you should buy"), "advice or prediction"),
    (("news from Reuters", "news from Reuters; the stock will rise tomorrow"), "advice or prediction"),
    (("ACME", "ACME and XYZCORP"), "name or ticker not in the data: XYZCORP"),
    (("up 8.91%", "down 8.91%"), "direction contradicts the data (the price rose)"),
])
def test_validator_rejects_invented_or_unsafe_content(change, fragment):
    problems = A.validate_text(GOOD.replace(*change), PK["full"])
    assert any(fragment in x for x in problems), problems


def test_validator_demands_key_figures_possible_wording_and_brevity():
    p = PK["full"]
    assert any("missing key figure: Official close" in x for x in A.validate_text(GOOD.replace("₹110.00", "the latest price"), p))
    assert any("missing key figure: Change" in x for x in A.validate_text(GOOD.replace("up 8.91% from", "from"), p))
    assert any("possible contributing factors" in x for x in A.validate_text(GOOD.replace("Possible contributing", "Notable"), p))
    assert any("too long" in x for x in A.validate_text("\n".join(["Line."] * 11), p))
    assert A.validate_text("", p) == ["empty text"]
    fell = assembler(build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (90.0, 101.0)})).why_moved("acme")
    assert any("price fell" in x for x in A.validate_text("Acme closed at ₹90.00, up 10.89% on 9 Nov 2026.", fell))


def test_required_caveats_must_survive_rewriting():
    none, text = PK["none"], "Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from ₹101.00."
    assert any("no clear evidence found" in x for x in A.validate_text(text, none))
    assert A.validate_text(text + " No clear evidence found between the 6 Nov and 9 Nov closes.", none) == []
    assert any("unlisted split" in x for x in A.validate_text("Acme closed at ₹10.00, down 90.10% on 9 Nov 2026.", PK["big"]))
    assert any("corporate action" in x for x in A.validate_text("Acme closed at ₹50.50 on 9 Nov 2026.", PK["bonus"]))
    assert any("could not be checked" in x for x in A.validate_text(
        "Acme closed at ₹110.00 on 9 Nov 2026, up 8.91% from ₹101.00.", PK["failed"]))


def test_clock_times_urls_and_list_numbers_do_not_confuse_the_number_check():
    p = PK["full"]
    with_url = GOOD + "\nSee https://nsearchives.nseindia.com/x2026.pdf"
    assert A.validate_text(with_url, p) == []
    numbered = "1. " + GOOD.replace("\n", "\n2. ")
    assert A.validate_text(numbered, p) == []


# ================================================================ footer and writer input
def test_footer_is_built_from_the_packet_only():
    f = A.build_footer(PK["full"])
    assert f.splitlines()[0].startswith("Sources: NSE end-of-day file")
    assert "Google News headlines" in f and "News: Reuters" not in f
    assert f.count("Link: ") == 3 and "https://nsearchives.nseindia.com/x.pdf" in f and "https://news/1" in f
    assert "As of 10 Nov 2026, 09:00 IST." in f and "not live quotes" in f and f.endswith("Information only, not investment advice.")
    bare = Packet("move", "ok", "X", "X Ltd", "not a date")
    assert A.build_footer(bare).splitlines() == ["Information only, not investment advice."]      # no prices, no price note


def test_summary_input_has_only_facts_and_context():
    payload = json.loads(A.summary_input(PK["full"]).split("Packet:\n", 1)[1].rsplit("\nWrite", 1)[0])
    assert set(payload) == {"company", "symbol", "facts", "market_context"}
    assert payload["facts"][0] == {"label": "Official close, 9 Nov 2026", "value": "₹110.00", "date": "9 Nov 2026"}
    text = A.summary_input(PK["full"])
    assert "http" not in text and "Analyst Meet" not in text and "caveat" not in text and len(text) < 1500


def test_select_factors_mixes_types_and_keeps_time_order():
    mk = lambda kind, when: Factor(kind, when, "t", f"{kind} {when}", "s")                     # noqa: E731
    fs = [mk("announcement", "2026-11-06T19:00:00"), mk("announcement", "2026-11-09T10:00:00"),
          mk("announcement", "2026-11-09T11:00:00"), mk("news", "2026-11-09T08:00:00+05:30"),
          mk("corporate_action", "2026-11-09"), mk("board_meeting", "2026-11-09")]
    picked = A.select_factors(fs, 4)
    assert sorted(f.kind for f in picked) == ["announcement", "board_meeting", "corporate_action", "news"]
    assert [f.when for f in picked] == sorted(f.when for f in picked)
    assert next(f for f in picked if f.kind == "announcement").when == "2026-11-09T11:00:00"   # the latest filing
    assert len(A.select_factors(fs, 2)) == 2 and A.select_factors([], 4) == []


def test_caveats_are_never_cut_when_the_message_is_crowded():
    gaps = tuple(Gap(c, t) for c, t in (
        ("announcements_unavailable", "NSE announcements could not be checked."),
        ("board_meetings_unavailable", "NSE board meetings could not be checked."),
        ("news_unavailable", "News could not be checked."),
        ("flows_unavailable", "FII/DII figures could not be fetched."),
        ("data_may_be_stale", "The latest stored close is 4 Nov 2026; newer data may exist.")))
    facts = (Fact("Official close, 4 Nov 2026", "₹97.00", 97.0, date="2026-11-04"),
             Fact("Previous close", "₹100.00", 100.0, date="2026-11-03"),
             Fact("Change from previous close", "-3.00%", -3.0, date="2026-11-04"))
    ctx = (Fact("Median change across 300 listed stocks", "+0.10%", 0.1, date="2026-11-04"),
           Fact("Stocks up / down", "160 / 140", None, date="2026-11-04"),
           Fact("DII net buying/selling (Rs crore)", "+10.00", 10.0, date="2026-11-04"))
    factors = tuple(Factor("announcement", f"2026-11-04T1{i}:00:00", "during market hours", f"Filing {i}", "NSE announcement")
                    for i in range(6))
    p = Packet("move", "ok", "ACME", "Acme Ltd", "2026-11-10T09:00:00+05:30", facts, ctx, factors, gaps)
    text = A.fallback_text(p)
    assert len([l for l in text.splitlines() if l.strip()]) <= A.MAX_LINES
    for g in gaps:
        assert A.REQUIRED_PHRASES[g.code] in text.lower()
    assert A.validate_text(text, p) == []


def test_extreme_crowding_still_keeps_every_caveat_and_one_hedged_factor():
    gaps = tuple(Gap(c, t) for c, t in (
        ("announcements_unavailable", "NSE announcements could not be checked."),
        ("board_meetings_unavailable", "NSE board meetings could not be checked."),
        ("news_unavailable", "News could not be checked."),
        ("flows_unavailable", "FII/DII figures could not be fetched."),
        ("data_may_be_stale", "The latest stored close is 4 Nov 2026; newer data may exist."),
        ("unverified_large_move", "The price changed by -30.00% but NSE's data lists no matching corporate action; "
                                   "it may be an unlisted split or unit change.")))
    facts = (Fact("Official close, 4 Nov 2026", "₹70.00", 70.0, date="2026-11-04"),
             Fact("Previous close", "₹100.00", 100.0, date="2026-11-03"),
             Fact("Change from previous close", "-30.00%", -30.0, date="2026-11-04"))
    ctx = (Fact("Median change across 300 listed stocks", "+0.10%", 0.1, date="2026-11-04"),
           Fact("DII net buying/selling (Rs crore)", "+10.00", 10.0, date="2026-11-04"))
    fs = (Factor("announcement", "2026-11-04T11:00:00", "during market hours", "Filing", "NSE announcement"),)
    p = Packet("move", "ok", "ACME", "Acme Ltd", "2026-11-10T09:00:00+05:30", facts, ctx, fs, gaps)
    text = A.fallback_text(p)
    assert len([l for l in text.splitlines() if l.strip()]) <= A.MAX_LINES
    assert "Possible contributing factor (not a confirmed cause)" in text
    assert all(A.REQUIRED_PHRASES[g.code] in text.lower() for g in gaps) and A.validate_text(text, p) == []


# ================================================================ the hybrid move answer
SUMMARY = ("Acme Industries Limited (ACME) closed at ₹110.00 on 9 Nov 2026, up 8.91% from the previous close of ₹101.00.\n"
           "The median listed stock moved +0.50% that day, with 151 stocks up and 50 down.")


def test_a_good_summary_is_accepted_in_any_date_and_number_format():
    p = PK["full"]
    assert A.validate_summary(SUMMARY, p) == []
    for old, new in (("9 Nov 2026", "9 November 2026"), ("9 Nov 2026", "Nov 9"), ("9 Nov 2026", "2026-11-09"),
                     ("₹110.00", "₹110"), ("₹101.00", "Rs 101.0")):
        assert A.validate_summary(SUMMARY.replace(old, new), p) == [], old


@pytest.mark.parametrize("change,fragment", [
    ((" on 9 Nov 2026", ""), "missing key figure: the date of the close"),
    (("₹110.00", "the latest price"), "missing key figure: the official close"),
    (("up 8.91% from", "from"), "missing key figure: the change"),
    (("with 151 stocks", "with news about 151 stocks"), "must not discuss factors, filings or news"),
    (("moved +0.50%", "moved +0.50% because of a filing"), "causal wording"),
    (("up 8.91%", "up 9.5%"), "number not in the data: 9.5%"),
    (("up 8.91%", "down 8.91%"), "direction contradicts the data"),
    (("(ACME)", "(XYZCORP)"), "name or ticker not in the data: XYZCORP"),
    (("that day", "that day at 3:30 PM"), "clock time"),
    (("9 Nov 2026", "10 Nov 2026"), "date not in the data: 10 Nov 2026"),
    (("Acme Industries", "You should buy Acme Industries"), "advice or prediction"),
])
def test_summary_validator_rejects_wrong_and_missing_content(change, fragment):
    problems = A.validate_summary(SUMMARY.replace(*change), PK["full"])
    assert any(fragment in x for x in problems), problems


def test_summary_validator_scope_and_length():
    assert A.validate_summary("a\nb\nc\nd", PK["full"])[0].startswith("summary too long")
    assert A.validate_summary("", PK["full"]) == ["empty text"]
    assert A.validate_summary(SUMMARY, PK["return"]) == ["not a move packet"]
    assert A.validate_summary(SUMMARY, PK["refused"]) == ["not a move packet"]
    ex_date = "Acme Industries Limited (ACME) closed at ₹50.50 on 9 Nov 2026; the previous close was ₹101.00 (unadjusted)."
    assert A.validate_summary(ex_date, PK["bonus"]) == []                    # no change figure to demand on an ex-date


def test_compose_move_adds_factors_and_caveats_in_code():
    composed = A.compose_move(PK["full"], SUMMARY)
    lines = composed.splitlines()
    assert lines[:2] == SUMMARY.splitlines() and lines[2] == "Possible contributing factors (not confirmed causes):"
    assert "News (Reuters), after the previous close, before the open:" in composed and "Outcome of Board Meeting" in composed
    assert A.validate_text(composed, PK["full"]) == [] and len(lines) <= A.MAX_LINES
    none = A.compose_move(PK["none"], SUMMARY)
    assert none.splitlines()[-1].startswith("No clear evidence found.") and A.validate_text(none, PK["none"]) == []
    bonus = A.compose_move(PK["bonus"], "Acme Industries Limited (ACME) closed at ₹50.50 on 9 Nov 2026; the previous "
                           "close was ₹101.00 (unadjusted).")
    assert bonus.count("Bonus 1:1") == 1 and "Possible contributing" not in bonus      # stated once, as a verified fact
    assert A.validate_text(bonus, PK["bonus"]) == []


def test_a_verified_ex_date_is_not_repeated_as_a_hedged_factor():
    assert A.shown_candidates(PK["bonus"]) == [] and A.shown_candidates(PK["full"]) == list(PK["full"].factors)
    assert "Possible contributing" not in A.fallback_text(PK["bonus"])


def test_premise_notes_when_the_question_assumes_the_wrong_direction():
    rose, none = PK["full"], PK["none"]
    assert A.premise_note("why is acme down today", rose) == [
        "Note: the change from the previous close was +8.91%, which is not a decrease."]
    assert A.premise_note("why is acme up today", rose) == [] and A.premise_note("what moved acme", rose) == []
    assert A.premise_note("is acme up or down", rose) == []                                 # both directions asked
    fell = assembler(build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (90.0, 101.0)})).why_moved("acme")
    assert A.premise_note("why is acme up", fell)[0].endswith("which is not an increase.")
    flat = assembler(build_store({"2026-11-06": (101.0, 100.0), "2026-11-09": (101.0, 101.0)})).why_moved("acme")
    assert "+0.00%" in A.premise_note("why is acme up today", flat)[0]
    assert A.premise_note("why is acme up", PK["refused"]) == [] and A.premise_note("why up", PK["return"]) == []
    composed = A.compose_move(fell, "Acme Industries Limited (ACME) closed at ₹90.00 on 9 Nov 2026, down 10.89% from the "
                              "previous close of ₹101.00.", A.premise_note("why is acme up", fell))
    assert composed.splitlines()[1].startswith("Note:") and A.validate_text(composed, fell) == []


def test_filing_lines_show_the_detail_when_the_category_alone_says_nothing():
    detail = "The company has informed the Exchange about a press release on investor day"
    assert len(detail) <= 80
    f = Factor("announcement", "2026-11-12T11:19:40", "on 12 Nov 2026", "Updates", "NSE announcement", detail)
    assert A._factor_line(f) == f"- NSE filing, on 12 Nov 2026: Updates - {detail}"
    long = Factor("announcement", "2026-11-12T11:19:40", "on 12 Nov 2026", "Updates", "NSE announcement", detail + " and more words")
    assert A._factor_line(long).endswith("…") and len(A._factor_line(long)) < 140          # detail is clipped at a word
    same = Factor("announcement", "2026-11-04T20:47:14", "on 4 Nov 2026", "Credit Rating", "NSE announcement",
                  "Reliance Industries Limited has informed the Exchange about Credit Rating")
    assert A._factor_line(same) == "- NSE filing, on 4 Nov 2026: Credit Rating"              # detail repeats the title: not added


def test_footer_links_html_mode_long_links_publishers_and_price_note():
    long_url = "https://news.google.com/rss/articles/" + "A" * 300
    fs = (Factor("news", "2026-11-09T08:00:00+05:30", "x", "Tom & Jerry <b>", "News: Reuters", url="https://a.example/x?a=1&b=2"),
          Factor("news", "2026-11-09T09:00:00+05:30", "x", "Long one", "News: Mint", url=long_url))
    p = Packet("move", "ok", "X", "X", "2026-11-10T09:00:00+05:30", factors=fs)
    plain = A.build_footer(p)
    assert "Link: Tom & Jerry <b> (Reuters) https://a.example/x?a=1&b=2" in plain
    assert "Link: Long one (Mint) (long link not shown in plain text)" in plain and long_url not in plain
    html_footer = A.build_footer(p, html_links=True)
    assert 'Link: <a href="https://a.example/x?a=1&amp;b=2">Tom &amp; Jerry &lt;b&gt; (Reuters)</a>' in html_footer
    assert f'<a href="{long_url}">Long one (Mint)</a>' in html_footer
    assert "Prices are NSE" not in plain and "Prices are NSE" in A.build_footer(PK["full"])
    assert "As of 10 Nov 2026, 09:00 IST." in plain


def test_footer_describes_only_the_factors_the_message_displayed():
    import dataclasses
    fs = tuple(Factor("announcement", f"2026-11-09T0{i}:00:00", "during market hours", f"Filing {i}", "NSE announcement",
                      url=f"https://nsearchives.nseindia.com/f{i}.pdf") for i in range(1, 7)) + (
        Factor("news", "2026-11-09T07:30:00+05:30", "x", "Reliance wins order", "News: Reuters", url="https://news/r"),)
    p = dataclasses.replace(PK["full"], factors=fs)
    shown = []
    text = A.compose_move(p, SUMMARY, (), shown)
    assert 1 <= len(shown) <= A.MAX_FACTORS_SHOWN and all(f.title in text for f in shown)
    hidden = [f for f in fs if f not in shown]
    assert hidden                                                         # something was left out
    footer = A.build_footer(p, factors=shown)
    for f in shown:
        assert f.title not in footer or f.url in footer                   # shown factors may be linked
    for f in hidden:
        assert f.url not in footer                                        # hidden ones never are
    assert ("Google News headlines" in footer) == any(f.kind == "news" for f in shown)   # sources follow the display
    code_shown = []
    A.fallback_text(p, (), code_shown)
    assert code_shown == shown
    assert A.build_footer(p).count("Link: ") == 3                         # default behaviour is unchanged
    events_shown = []
    ev_text = A.fallback_text(PK["events"], (), events_shown)
    assert events_shown and all(f.title in ev_text for f in events_shown)
    assert A.fallback_text(PK["refused"], (), (empty := [])) and empty == []
