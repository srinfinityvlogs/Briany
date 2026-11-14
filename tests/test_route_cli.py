from datetime import date

import pytest

from briany.route_cli import build_suite, matches, short
from briany.router import TOOLS, Context, Route, rule_route
from test_router import RESOLVER

TODAY = "2026-11-10"
VALID = {t["name"] for t in TOOLS}


def test_suite_is_well_formed_and_dates_follow_today():
    suite = build_suite(TODAY)
    assert len(suite) == 25 and len({q for q, _e, _c in suite}) == 25
    assert all(e["tool"] in VALID for _q, e, _c in suite)
    dates = {q: e["date"] for q, e, _c in suite if "date" in e}
    assert dates["what happened to TCS yesterday"] == "2026-11-09"
    assert dates["Why did Reliance fall on 5 Nov?"] == "2026-11-05"
    early = dict((q, e) for q, e, _c in build_suite("2026-03-01"))
    assert early["Why did Reliance fall on 5 Nov?"]["date"] == "2025-11-05"            # not yet 5 Nov this year


def test_matches_checks_tool_query_period_date_and_reason():
    exp = {"tool": "period_return", "query_has": "tcs", "period": "6m"}
    assert matches(exp, Route("period_return", {"query": "TCS", "period": "6m"}))
    assert not matches(exp, Route("period_return", {"query": "TCS", "period": "1m"}))
    assert not matches(exp, Route("period_return", {"query": "infosys", "period": "6m"}))
    assert not matches(exp, Route("events", {"query": "tcs"})) and not matches(exp, None)
    assert matches({"tool": "unsupported", "reason": "advice"}, Route("unsupported", {"reason": "advice"}))
    assert short(Route("why_moved", {"query": "tcs", "date": "2026-11-05"})) == "why_moved query=tcs date=2026-11-05"
    assert short(None) == "-"


def test_rule_router_is_a_usable_fallback_on_the_real_suite():
    """Guard against regressions: the fallback must keep handling the common question shapes."""
    suite = build_suite(TODAY)
    passed = [q for q, e, c in suite if matches(e, rule_route(q, RESOLVER, TODAY, c))]
    failed = [q for q, e, c in suite if q not in passed]
    assert len(passed) >= 25, f"rule router passes only {len(passed)}/25; failing: {failed}"
