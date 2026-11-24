# SPDX-License-Identifier: Apache-2.0
"""Step 14: index day close. Real NSE files (3 Nov and 30 Oct 2026) are the fixtures; the 10 Nov 'file' is the HTML page NSE
really served for a day without a file (HTTP 200, not 404)."""
import dataclasses
import io
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from briany.answer import build_footer, fallback_text, premise_note, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler, _index_row_problem
from briany.indexfile import IndexClient, IndexFileError, IndexRow, find_row, parse_index_csv
from briany.llm import Chain, LlmError
from briany.marketdata import PriceRow
from briany.nse import MemoryCache
from briany.router import Context, rule_route, route_question
from briany.store import MemoryStore
from briany.symbols import Instrument, Resolver, find_index
from test_evidence import FakeNews, FakeNse

FIX = Path(__file__).parent / "fixtures" / "indices"
CSV_NOV3 = (FIX / "ind_close_all_03112026.csv").read_text(encoding="utf-8")
CSV_OCT30 = (FIX / "ind_close_all_30102026.csv").read_text(encoding="utf-8")
HTML_PAGE = ('\n<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">\n'
             "    <title>Service Temporarily Unavailable</title>\n</head>\n<body>Service Temporarily Unavailable</body>\n</html>\n")
RESOLVER = Resolver([Instrument("TCS", "TATA CONSULTANCY SERV LT"), Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD")])
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=IST)                 # latest stored close: 3 Nov


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


NOTHING = {"00000000": ""}          # a truthy file map that holds no real day: every request gets NSE's HTML page


class Opener:
    """Serves the files by date; anything else answers the way NSE does for a day without a file."""

    def __init__(self, files=None, errors=None):
        self.files = files or {"03112026": CSV_NOV3, "30102026": CSV_OCT30}
        self.errors, self.urls = list(errors or []), []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.errors:
            raise self.errors.pop(0)
        stamp = req.full_url.rsplit("_", 1)[1][:8]
        return Resp(self.files.get(stamp, HTML_PAGE).encode("utf-8"))


def store(*days):
    s = MemoryStore()
    for d in days:
        s.put_day(d, {"TCS": PriceRow(100.0, 99.0, 10)})
    return s


def asm(days=("2026-10-30", "2026-11-03"), opener=None, now=NOW, index=True):
    client = IndexClient(cache=MemoryCache(), opener=opener or Opener(), sleep=lambda s: None) if index else None
    return Assembler(store(*days), FakeNse(), FakeNews(), RESOLVER, now=lambda: now, index=client)


# ================================================================ parser
def test_real_file_parses_with_nse_number_quirks():
    rows = parse_index_csv(CSV_NOV3, "2026-11-03")
    assert len(rows) == 167
    assert find_row(rows, "nifty 50") == IndexRow("Nifty 50", "2026-11-03", 23582.25, 23810.85, 23482.8, 23810.85, 370.05,
                                                  1.58, 19.71, 2.92, 1.46)
    bank = find_row(rows, "Nifty Bank")
    assert bank.pct == 0.98 and bank.points == 537.85                         # '.98' in the file
    assert find_row(rows, "NIFTY MIDCAP 100").close == 58596.25
    vix = find_row(rows, "India VIX")
    assert vix.high == 16.2225 and vix.pe is None and vix.dy is None         # '-' means not applicable
    div = find_row(rows, "Nifty50 Dividend Points")
    assert div.pct is None and div.open is None and div.close == 206.19


def test_a_negative_quirk_and_the_other_real_day():
    rows = parse_index_csv(CSV_OCT30, "2026-10-30")
    bank = find_row(rows, "Nifty Bank")
    assert (bank.points, bank.pct) == (-222.7, -0.41)                         # '-.41' in the file


def test_index_names_are_matched_by_full_key_so_private_bank_is_not_bank():
    rows = parse_index_csv(CSV_NOV3, "2026-11-03")
    assert find_row(rows, "Nifty Private Bank").name == "Nifty Private Bank"
    assert find_row(rows, "Nifty Bank").name == "Nifty Bank"
    assert find_index("nifty private bank").name == "Nifty Private Bank"
    assert find_index("Nifty Bank").name == "Nifty Bank" and find_index("bank nifty").name == "Nifty Bank"


@pytest.mark.parametrize("body,day,why", [
    (HTML_PAGE, "2026-11-10", "not a CSV"),
    ("", "2026-11-03", "not a CSV"),
    (CSV_NOV3, "2026-11-04", "different day"),
    ("Index Name,Index Date\nNifty 50,03-11-2026\n", "2026-11-03", "unexpected columns"),
    ("\n".join(CSV_NOV3.splitlines()[:20]) + "\n", "2026-11-03", "incomplete"),
    (CSV_NOV3.replace("23810.85,370.05", "nan,370.05", 1), "2026-11-03", "unreadable number"),
    (CSV_NOV3.replace("23810.85,370.05", "23,810.85,370.05", 1), "2026-11-03", "wrong number of columns"),
    (CSV_NOV3.replace("03-11-2026", "31-02-2026", 1), "2026-11-03", "date"),
])
def test_anything_but_the_complete_file_for_that_day_fails_closed(body, day, why):
    with pytest.raises(IndexFileError, match=why):
        parse_index_csv(body, day)


def test_bom_and_identical_duplicate_rows_are_tolerated_but_conflicting_duplicates_are_not():
    assert len(parse_index_csv("\ufeff" + CSV_NOV3, "2026-11-03")) == 167
    lines = CSV_NOV3.splitlines()
    assert len(parse_index_csv("\n".join(lines + [lines[1]]), "2026-11-03")) == 167
    conflicting = lines[1].replace("23810.85", "23999.00")
    with pytest.raises(IndexFileError, match="twice"):
        parse_index_csv("\n".join(lines + [conflicting]), "2026-11-03")


# ================================================================ client
def test_client_fetches_once_then_serves_from_cache():
    op = Opener()
    c = IndexClient(cache=MemoryCache(), opener=op, sleep=lambda s: None)
    a, b = c.day("2026-11-03"), c.day("2026-11-03")
    assert a == b and len(a) == 167 and len(op.urls) == 1
    assert op.urls[0].endswith("/ind_close_all_03112026.csv")


def test_the_html_page_with_status_200_is_an_error_and_is_never_cached():
    op, cache = Opener(), MemoryCache()
    c = IndexClient(cache=cache, opener=op, sleep=lambda s: None)
    with pytest.raises(IndexFileError):
        c.day("2026-11-10")
    assert cache.get("idx:2026-11-10") is None
    with pytest.raises(IndexFileError):
        c.day("2026-11-10")
    assert len(op.urls) == 2                                                  # asked again: a failure is not remembered


def test_http_errors_network_errors_and_one_retry_on_5xx():
    http = lambda code: urllib.error.HTTPError("u", code, "x", {}, None)     # noqa: E731
    with pytest.raises(IndexFileError, match="HTTP 404"):
        IndexClient(opener=Opener(errors=[http(404)]), sleep=lambda s: None).day("2026-11-03")
    assert len(IndexClient(opener=Opener(errors=[http(503)]), sleep=lambda s: None).day("2026-11-03")) == 167
    with pytest.raises(IndexFileError, match="HTTP 503"):
        IndexClient(opener=Opener(errors=[http(503), http(503)]), sleep=lambda s: None).day("2026-11-03")
    with pytest.raises(IndexFileError, match="network error"):
        IndexClient(opener=Opener(errors=[urllib.error.URLError("x")] * 2), sleep=lambda s: None).day("2026-11-03")
    with pytest.raises(IndexFileError, match="bad date"):
        IndexClient(opener=Opener()).day("not-a-date")


def test_a_damaged_cache_entry_is_ignored_and_a_broken_cache_never_hides_the_answer():
    cache = MemoryCache()
    cache.set("idx:2026-11-03", [["only", "junk"]], 60)
    op = Opener()
    assert len(IndexClient(cache=cache, opener=op).day("2026-11-03")) == 167 and len(op.urls) == 1

    class Broken:
        def get(self, k):
            raise RuntimeError("redis down")

        def set(self, k, v, ttl):
            raise RuntimeError("redis down")
    assert len(IndexClient(cache=Broken(), opener=Opener()).day("2026-11-03")) == 167


# ================================================================ the packet and the message
def test_latest_close_of_nifty_50_is_built_from_the_file():
    p = asm().index_close("nifty")
    assert (p.kind, p.status, p.symbol, p.name) == ("index", "ok", "NIFTY 50", "Nifty 50")
    f = {x.label: x for x in p.facts}
    assert f["Closing level, 3 Nov 2026"].raw == 23810.85 and f["Points change"].value == "+370.05"
    assert f["Change from previous close"].value == "+1.58%" and f["Dividend yield"].value == "1.46%"
    assert p.gaps == () and p.evidence_complete and p.factors == ()
    text = fallback_text(p)
    assert text.splitlines()[0] == "Nifty 50 closed at 23,810.85 on 3 Nov 2026, up 370.05 points (+1.58%) from the previous close."
    assert "Open 23,582.25; high 23,810.85; low 23,482.80." in text
    assert "NSE publishes P/E 19.71; P/B 2.92; dividend yield 1.46%." in text
    assert validate_text(text, p) == []
    footer = build_footer(p)
    assert footer.startswith("Sources: NSE end-of-day index file") and "Index levels are NSE official end-of-day values, not live." in footer
    assert "Prices are NSE" not in footer


def test_a_down_day_a_named_date_and_vix():
    p = asm().index_close("bank nifty", on="2026-10-30")
    assert fallback_text(p).splitlines()[0] == ("Nifty Bank closed at 54,686.95 on 30 Oct 2026, down 222.70 points "
                                                "(-0.41%) from the previous close.")
    v = asm().index_close("vix")
    t = fallback_text(v)
    assert t.splitlines()[0].startswith("India VIX closed at 15.72 on 3 Nov 2026, up 0.22 points (+1.44%)")
    assert "P/E" not in t and validate_text(t, v) == []                      # '-' fields are left out, not shown as 0


def test_every_index_in_both_real_files_gives_an_answer_that_passes_the_validator():
    checked = 0
    for day, csv_text in (("2026-11-03", CSV_NOV3), ("2026-10-30", CSV_OCT30)):
        a = asm(opener=Opener({day[8:] + day[5:7] + day[:4]: csv_text}))
        for row in parse_index_csv(csv_text, day):
            p = a.index_close(row.name, on=day)
            text = fallback_text(p, premise_note("why is it up", p))
            assert p.status in ("ok", "refused"), row.name
            assert validate_text(text, p) == [], (row.name, text, validate_text(text, p))
            checked += 1
    assert checked == 334


def test_an_index_without_a_change_figure_shows_the_close_and_says_so():
    p = asm().index_close("Nifty50 Dividend Points")
    assert p.status == "ok" and [g.code for g in p.gaps] == ["index_change_missing"]
    text = fallback_text(p)
    assert text.splitlines()[0] == "Nifty50 Dividend Points closed at 206.19 on 3 Nov 2026." and "no change figure" in text
    assert validate_text(text, p) == []


def test_when_neither_the_latest_nor_the_previous_file_exists_it_is_a_gap_not_a_guess():
    a = asm(days=("2026-10-30", "2026-11-03", "2026-11-10"), opener=Opener(NOTHING), now=datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    p = a.index_close("nifty")                                               # nothing is served for any stored day
    assert p.status == "refused" and p.facts == () and [g.code for g in p.gaps] == ["index_file_unavailable"]
    text = fallback_text(p)
    assert "could not be fetched" in text and "10 Nov 2026" in text and "23,810" not in text
    assert text.startswith("I can't give a reliable figure for Nifty 50.")
    assert validate_text(text, p) == []
    named = asm(days=("2026-10-30", "2026-11-03", "2026-11-10"), now=datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    assert named.index_close("nifty", on="2026-11-10").status == "refused"   # a day the user NAMED is never swapped for another


def test_a_latest_file_that_is_not_out_falls_back_to_the_previous_trading_day_and_says_so():
    a = asm(days=("2026-10-30", "2026-11-03", "2026-11-10"), now=datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    p = a.index_close("nifty")                                               # 10 Nov has no file (HTML page); 3 Nov has
    assert p.status == "ok" and p.facts[0].date == "2026-11-03"
    assert [g.code for g in p.gaps] == ["data_may_be_stale", "index_latest_missing"]       # 3 Nov is a week old by 10 Nov
    text = fallback_text(p)
    assert text.startswith("Nifty 50 closed at 23,810.85 on 3 Nov 2026")
    assert "NSE's index file for 10 Nov 2026 is not available yet, so this is the previous trading day, 3 Nov 2026." in text
    assert validate_text(text, p) == []


@pytest.mark.parametrize("query,on,code", [
    ("nifty", "2026-11-01", "no_session"),            # between stored days: weekend or holiday
    ("nifty", "2026-11-09", "no_index_data"),         # after the latest stored day
    ("nifty", "2026-10-01", "no_index_data"),         # before the earliest stored day
    ("nifty", "banana", "bad_date"),
    ("Nifty Waves", "2026-11-03", None),              # a real name; present in the file, so it works
])
def test_date_edges(query, on, code):
    p = asm().index_close(query, on=on)
    if code is None:
        assert p.status == "ok"
    else:
        assert p.status == "refused" and p.gaps[0].code == code and p.facts == ()
        assert validate_text(fallback_text(p), p) == []


def test_unknown_index_asks_and_missing_client_and_missing_row_refuse():
    c = asm().index_close("nifty xyz")
    assert (c.kind, c.status, c.gaps[0].code) == ("clarify", "clarify", "index_unresolved")
    assert "Nifty 50" in fallback_text(c)
    assert asm(index=False).index_close("nifty").gaps[0].code == "index_file_unavailable"
    short = "\n".join(l for l in CSV_NOV3.splitlines() if not l.startswith("Nifty Waves"))
    gone = asm(opener=Opener({"03112026": short})).index_close("nifty waves")
    assert gone.status == "refused" and gone.gaps[0].code == "index_not_in_file"


def test_inconsistent_rows_are_refused():
    row = find_row(parse_index_csv(CSV_NOV3, "2026-11-03"), "Nifty 50")
    assert _index_row_problem(row) == ""
    assert "differ" in _index_row_problem(dataclasses.replace(row, pct=2.58))
    assert "range" in _index_row_problem(dataclasses.replace(row, high=23000.0))
    assert "positive" in _index_row_problem(dataclasses.replace(row, points=999999.0))
    bad = CSV_NOV3.replace("23810.85,370.05,1.58", "23810.85,370.05,2.58", 1)
    p = asm(opener=Opener({"03112026": bad})).index_close("nifty")
    assert p.status == "refused" and p.gaps[0].code == "index_data_inconsistent" and p.facts == ()


def test_the_validator_catches_a_wrong_index_message():
    p = asm().index_close("nifty")
    good = fallback_text(p)
    assert validate_text(good.replace("23,810.85", "23,810.86"), p)          # an invented close
    assert validate_text(good.replace("up 370.05", "down 370.05"), p)         # wrong direction
    assert any("missing key figure" in x for x in validate_text("Nifty 50 closed on 3 Nov 2026.", p))
    assert validate_text(good + " It rose because of banks.", p)              # a cause


def test_why_questions_get_the_facts_and_one_plain_line_never_a_cause():
    p = asm().index_close("nifty")
    notes = premise_note("why is the nifty down today", p)
    assert notes[0].startswith("Note: the change from the previous close was +1.58%, which is not a decrease.")
    assert notes[1] == "I can show where an index closed, but I don't explain index moves."
    text = fallback_text(p, notes)
    assert validate_text(text, p) == [] and len(text.splitlines()) <= 10
    assert premise_note("how is the nifty", p) == []


# ================================================================ routing and the whole bot
def test_sensex_and_returns_and_follow_ups_route_in_code():
    r = lambda q, c=None: rule_route(q, RESOLVER, "2026-11-04", c)           # noqa: E731
    assert r("sensex today").args == {"reason": "index_not_covered"}
    assert r("nifty 1 month return").args == {"query": "nifty", "period": "1m"}
    assert r("nifty 1 month return").tool == "period_return"
    assert r("nifty it today").args == {"query": "nifty it"}
    assert r("and on 3 nov?", Context("NIFTY BANK", "Nifty Bank")).args == {"query": "NIFTY BANK", "date": "2026-11-03"}
    assert r("and last month?", Context("NIFTY BANK", "Nifty Bank")).args == {"query": "NIFTY BANK", "period": "1m"}
    assert r("what are FIIs doing", Context("NIFTY BANK", "Nifty Bank")).tool == "fii_dii"        # not a follow-up on the index
    assert r("tcs today", Context("NIFTY BANK", "Nifty Bank")).tool == "why_moved"


class Prov:
    def __init__(self, tool, args):
        self.name, self.tool, self.args = "groq", tool, args

    def route(self, system, user, tools):
        from briany.llm import ToolCall
        return ToolCall(self.tool, self.args)


def test_a_model_that_sends_an_index_to_a_stock_tool_is_corrected_in_code():
    def go(tool, args, q="nifty"):
        return route_question(q, Chain([Prov(tool, args)]), RESOLVER, "2026-11-04").route
    assert go("why_moved", {"query": "nifty bank", "date": "2026-11-03"}).tool == "index_close"
    assert go("period_return", {"query": "nifty", "period": "1m"}).tool == "period_return"     # an index return is offered now
    assert go("events", {"query": "nifty"}).tool == "unsupported"
    assert go("unsupported", {"reason": "market_data"}, "how is the nifty").tool == "index_close"
    assert go("unsupported", {"reason": "market_data"}, "what are FIIs doing").tool == "fii_dii"   # the model said unsupported
    assert go("unsupported", {"reason": "market_data"}, "what is the market wide delivery").args == {"reason": "market_data"}


def test_a_future_date_from_the_model_is_rejected_like_for_stocks():
    from briany.router import RouteError, validate_route
    with pytest.raises(RouteError):
        validate_route("index_close", {"query": "nifty", "date": "2026-12-01"}, "2026-11-04")
    assert validate_route("index_close", {"query": "nifty", "date": "2026-11-03"}, "2026-11-04").args == {
        "query": "nifty", "date": "2026-11-03"}


class DeadChain(Chain):
    def __init__(self):
        super().__init__([])

    def route(self, *a, **k):
        raise LlmError("all_failed", "all providers failed: groq=down")


def bot(**kw):
    return Bot(asm(**kw), DeadChain(), RESOLVER, now=lambda: NOW)


def test_end_to_end_without_any_model_the_question_is_answered_by_code():
    ans = bot().ask("how is the nifty doing")
    assert ans.kind == "index" and ans.writer == "code" and ans.route.tool == "index_close"
    assert ans.text.startswith("Nifty 50 closed at 23,810.85 on 3 Nov 2026, up 370.05 points (+1.58%)")
    assert "Sources: NSE end-of-day index file" in ans.text and ans.context == Context("NIFTY 50", "Nifty 50")
    assert validate_text(ans.body, ans.packet) == []
    follow = bot().ask("and on 30 oct?", ans.context)
    assert follow.kind == "index" and "Nifty 50 closed at 23,053.10 on 30 Oct 2026" in follow.text


def test_end_to_end_refusals_are_plain_and_keep_no_stock_footer():
    a = bot(days=("2026-10-30", "2026-11-03", "2026-11-10"), now=datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    ans = Bot(a.asm, DeadChain(), RESOLVER, now=lambda: datetime(2026, 11, 10, 20, 0, tzinfo=IST)).ask("nifty today")
    assert ans.kind == "index" and "closed at 23,810.85 on 3 Nov 2026" in ans.text and "previous trading day, 3 Nov 2026" in ans.text
    none = asm(days=("2026-10-30", "2026-11-03", "2026-11-10"), opener=Opener(NOTHING), now=datetime(2026, 11, 10, 20, 0, tzinfo=IST))
    ans = Bot(none, DeadChain(), RESOLVER, now=lambda: datetime(2026, 11, 10, 20, 0, tzinfo=IST)).ask("nifty today")
    assert ans.kind == "index" and "could not be fetched" in ans.text and "23,810" not in ans.text
    assert bot().ask("sensex today").text.startswith("I only have NSE indices")
    assert "Nifty 50 close today" in bot().ask("what is the index today").text


# ================================================================ regression-snapshot hooks
def test_index_calls_can_be_recorded_and_replayed_offline_including_failures():
    from briany.snapshot import Recorder, Replay
    rec = Recorder(IndexClient(opener=Opener(), sleep=lambda s: None))
    rows = rec.day("2026-11-03")
    with pytest.raises(IndexFileError):
        rec.day("2026-11-10")
    a = Assembler(store("2026-11-03", "2026-11-10"), FakeNse(), FakeNews(), RESOLVER,
                  now=lambda: datetime(2026, 11, 11, 9, 0, tzinfo=IST), index=Replay(_roundtrip(rec.calls)))
    assert a.index_close("nifty", on="2026-11-03").facts[0].raw == 23810.85
    assert a.index_close("nifty", on="2026-11-10").gaps[0].code == "index_file_unavailable"
    assert Replay(_roundtrip(rec.calls)).day("2026-11-03") == rows


def _roundtrip(calls):
    import json
    return json.loads(json.dumps(calls))
