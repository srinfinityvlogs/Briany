import json
import urllib.error

import fakeredis
import pytest

from briany import nse
from briany.nse import MemoryCache, NseClient, NseError, RedisCache


class FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(script):
    calls, reqs = [], []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        reqs.append(req)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResp(item)
    opener.calls, opener.reqs = calls, reqs
    return opener


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def client(script, cache=None, **kw):
    sleeps = []
    c = NseClient(cache=cache, min_interval=kw.pop("min_interval", 1.0), opener=make_opener(script),
                  sleep=sleeps.append, clock=kw.pop("clock", Clock()), **kw)
    return c, sleeps


def http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_url_and_headers():
    c, _ = client([b"[]"])
    c.corporate_actions(symbol="RELIANCE", start="2024-01-01", end="2026-11-06")
    assert c._open.calls == [nse.BASE + "corporates-corporateActions?index=equities&symbol=RELIANCE"
                             "&from_date=01-01-2024&to_date=06-11-2026"]
    req = c._open.reqs[0]
    assert "Mozilla" in req.get_header("User-agent") and req.get_header("Referer") == "https://www.nseindia.com/"


def test_throttle_waits_only_the_remaining_interval():
    clock = Clock()
    c, sleeps = client([b"[]", b"[]"], clock=clock)
    c.fii_dii()
    clock.t = 0.2
    c.fii_dii()
    assert sleeps == [pytest.approx(0.8)]


def test_cache_hit_avoids_second_request_and_params_split_keys():
    c, _ = client([b"[]", b"[]"], cache=MemoryCache())
    c.corporate_actions(symbol="AAA")
    c.corporate_actions(symbol="AAA")
    assert len(c._open.calls) == 1
    c.corporate_actions(symbol="BBB")
    assert len(c._open.calls) == 2


def test_large_responses_are_not_cached():
    big = json.dumps([{"symbol": "X", "exDate": "01-Jun-2026", "subject": "Dividend", "pad": "x" * 1000}] * 150).encode()
    assert len(big) > nse.MAX_CACHE_BYTES
    c, _ = client([big, big], cache=MemoryCache())
    c.corporate_actions(start="2026-01-01", end="2026-06-30")
    c.corporate_actions(start="2026-01-01", end="2026-06-30")
    assert len(c._open.calls) == 2


def test_server_errors_are_retried_once():
    c, sleeps = client([http(503), b"[]"])
    assert c.fii_dii() == []
    assert len(c._open.calls) == 2 and 2 in sleeps
    c2, _ = client([http(503), http(503)])
    with pytest.raises(NseError, match="HTTP 503"):
        c2.fii_dii()


def test_block_and_bad_pages_fail_loudly_without_retry():
    c, _ = client([http(403), b"[]"])
    with pytest.raises(NseError, match="HTTP 403"):
        c.fii_dii()
    assert len(c._open.calls) == 1
    c2, _ = client([b"<html>Access Denied</html>"])
    with pytest.raises(NseError, match="non-JSON"):
        c2.fii_dii()
    c3, _ = client([urllib.error.URLError("dns"), urllib.error.URLError("dns")])
    with pytest.raises(NseError, match="network error"):
        c3.fii_dii()
    c4, _ = client([b'{"unexpected": 1}'])
    with pytest.raises(NseError, match="unexpected NSE response"):
        c4.corporate_actions()


def test_memory_cache_expires():
    clock = Clock()
    cache = MemoryCache(clock=clock)
    cache.set("k", {"a": 1}, ttl=10)
    assert cache.get("k") == {"a": 1}
    clock.t = 11
    assert cache.get("k") is None and cache.get("missing") is None


def test_redis_cache_roundtrip_with_ttl():
    r = fakeredis.FakeRedis(decode_responses=True)
    cache = RedisCache(r)
    assert cache.get("k") is None
    cache.set("k", [{"a": 1}], ttl=60)
    assert cache.get("k") == [{"a": 1}] and 0 < r.ttl("cache:k") <= 60


def test_windowed_calendar_and_board_meeting_urls():
    c, _ = client([b"[]", b"[]"])
    c.event_calendar("RELIANCE", "2026-11-07", "2026-12-31")
    c.board_meetings("RELIANCE", "2026-07-01", "2026-11-06")
    assert c._open.calls == [
        nse.BASE + "event-calendar?index=equities&symbol=RELIANCE&from_date=07-11-2026&to_date=31-12-2026",
        nse.BASE + "corporate-board-meetings?index=equities&symbol=RELIANCE&from_date=01-07-2026&to_date=06-11-2026"]


def test_next_results_returns_event_or_none_for_not_announced():
    body = json.dumps([
        {"symbol": "ABC", "company": "ABC", "purpose": "Buyback", "bm_desc": "x", "date": "12-Nov-2026"},
        {"symbol": "ABC", "company": "ABC", "purpose": "Financial Results", "bm_desc": "x", "date": "20-Nov-2026"}]).encode()
    c, _ = client([body])
    ev = c.next_results("ABC", "2026-11-07")
    assert ev.date == "2026-11-20" and ev.purpose == "Financial Results"
    assert c._open.calls[0].endswith("from_date=07-11-2026&to_date=07-03-2027")         # 120-day horizon
    c2, _ = client([b"[]"])
    assert c2.next_results("ABC", "2026-11-07") is None


def rec(symbol, ex, subject):
    return {"symbol": symbol, "comp": symbol, "exDate": ex, "recDate": ex, "subject": subject, "series": "EQ", "isin": "X"}


def test_corporate_actions_all_merges_indexes_and_reports_each():
    equities = json.dumps([rec("AAA", "05-Mar-2026", "Bonus 1:1"), rec("DUP", "05-Mar-2026", "Dividend - Rs 1")]).encode()
    sme = json.dumps([rec("BBB", "06-Mar-2026", "Face Value Split - From Rs 10 To Rs 2"),
                      rec("DUP", "05-Mar-2026", "Dividend - Rs 1")]).encode()
    mf = json.dumps([rec("ETFX", "07-Mar-2026", "Face Value Split - From Rs 100 To Rs 10")]).encode()
    c, _ = client([equities, sme, mf])
    acts, report = c.corporate_actions_all(start="2026-03-01", end="2026-03-31")
    assert [a.symbol for a in acts] == ["AAA", "DUP", "BBB", "ETFX"]               # DUP not repeated
    assert report == {"equities": {"raw": 2, "parsed": 2}, "sme": {"raw": 2, "parsed": 2},
                      "mf": {"raw": 1, "parsed": 1}}
    assert [u.split("index=")[1].split("&")[0] for u in c._open.calls] == ["equities", "sme", "mf"]


def test_corporate_actions_all_reports_a_bad_extra_index_but_not_a_bad_first_one():
    good = json.dumps([rec("AAA", "05-Mar-2026", "Bonus 1:1")]).encode()
    c, _ = client([good, http(400), b'{"not": "a list"}'])
    acts, report = c.corporate_actions_all()
    assert len(acts) == 1 and "HTTP 400" in report["sme"]["error"] and "unexpected" in report["mf"]["error"]
    c2, _ = client([http(403)])
    with pytest.raises(NseError, match="HTTP 403"):
        c2.corporate_actions_all()
