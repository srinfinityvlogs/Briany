"""Synthetic feeds that follow the RSS 2.0 layout. Real Google output is verified separately (news_samples)."""
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import fakeredis
import pytest

from briany import news
from briany.news import NewsClient, NewsError
from briany.nse import MemoryCache, RedisCache

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)          # a Monday, 17:30 IST


def feed(items):
    body = "".join(
        f"<item><title>{escape(t)}</title><link>{l}</link><pubDate>{p}</pubDate>"
        f'<source url="{su}">{escape(s)}</source></item>' for t, l, p, s, su in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>q</title>{body}</channel></rss>'


def item(title, pub="Mon, 09 Nov 2026 05:30:00 GMT", source="Economic Times", link="https://news.google.com/rss/articles/x1",
         su="https://economictimes.indiatimes.com"):
    return (title, link, pub, source, su)


class FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self, n=-1):
        return self._b if n < 0 else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(script):
    calls = []

    def opener(req, timeout=None):
        calls.append(req.full_url)
        x = script.pop(0)
        if isinstance(x, Exception):
            raise x
        return FakeResp(x if isinstance(x, bytes) else x.encode())
    opener.calls = calls
    return opener


def client(script, cache=None):
    sleeps = []
    c = NewsClient(cache=cache, opener=make_opener(script), sleep=sleeps.append, clock=lambda: 0.0)
    return c, sleeps


def http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


# ---------------------------------------------------------------- query and name helpers
def test_query_and_tokens():
    assert news.clean_company_name("Tempsens Instruments (India) Limited") == "Tempsens Instruments"
    assert news.build_query("Reliance Industries Limited") == '"Reliance Industries" when:7d'
    assert news.name_tokens("Nippon India ETF Gold BeES") == ["nippon", "etf"]
    assert news.name_tokens("Tempsens Instruments (India) Ltd") == ["tempsens", "instruments"]
    assert news.name_tokens("") == []


def test_relevance_rules():
    tcs = news.name_tokens("Tata Consultancy Services Limited")
    assert news.is_relevant("Tata Consultancy Services bags deal", tcs, "TCS")
    assert news.is_relevant("TCS wins a large contract", tcs, "TCS")               # ticker alone is enough here
    assert not news.is_relevant("Tata Motors sales rise", tcs, "TCS")
    ril = news.name_tokens("Reliance Industries Limited")
    assert news.is_relevant("Reliance Industries and Jio news", ril, "RELIANCE")
    assert not news.is_relevant("Reliance Power wins contract", ril, "RELIANCE")   # ticker is a name word: no shortcut
    assert not news.is_relevant("Tata Steel results", news.name_tokens("Tata Motors Ltd"), "TATAMOTORS")


# ---------------------------------------------------------------- filtering
def test_filter_keeps_relevant_news_and_explains_every_drop():
    raw = news.parse_feed(feed([
        item("Reliance Industries Q2 profit rises 5% - Economic Times", "Mon, 09 Nov 2026 05:30:00 GMT"),
        item("Reliance Industries share price today live - Screener", source="Screener", su="https://www.screener.in"),
        item("Reliance Industries Share Price Live, stock quote - Moneycontrol", source="Moneycontrol"),
        item("Reliance Power wins contract - Mint", source="Mint"),
        item("Reliance Industries Q2 profit rises 5% - Mint", "Mon, 09 Nov 2026 04:00:00 GMT", source="Mint"),
        item("Reliance Industries old story - Mint", "Tue, 20 Oct 2026 05:00:00 GMT", source="Mint"),
        item("Reliance Industries odd date - Mint", "yesterday", source="Mint"),
        item("Reliance Industries from the future - Mint", "Thu, 12 Nov 2026 05:00:00 GMT", source="Mint"),
        item("Reliance Industries no link - Mint", link="", source="Mint"),
        item("Reliance Industries and Jio plan telecom IPO - Reuters", "Mon, 09 Nov 2026 03:00:00 GMT", source="Reuters"),
    ]))
    assert len(raw) == 10
    items, dropped = news.filter_items(raw, "Reliance Industries Limited", "RELIANCE", NOW)
    assert [i.title for i in items] == ["Reliance Industries Q2 profit rises 5%",
                                        "Reliance Industries and Jio plan telecom IPO"]
    assert items[0].published == "2026-11-09T11:00:00+05:30" and items[0].source == "Economic Times"
    assert dropped == {"blocked_source": 1, "quote_page": 1, "irrelevant": 1, "duplicate": 1, "old": 1,
                       "bad_date": 1, "future": 1, "incomplete": 1}


def test_limit_and_ordering():
    raw = [{"title": f"Tempsens Instruments update {i}", "source": "S", "source_url": "", "link": f"http://x/{i}",
            "pub": f"Mon, 09 Nov 2026 0{i}:00:00 GMT"} for i in range(1, 8)]
    items, _ = news.filter_items(raw, "Tempsens Instruments (India) Limited", "TEMPSENS", NOW, limit=3)
    assert [i.title[-1] for i in items] == ["7", "6", "5"]


# ---------------------------------------------------------------- client
def test_fetch_builds_the_phase1_url_and_returns_ok():
    c, _ = client([feed([item("Reliance Industries hits record - Mint", source="Mint")])])
    r = c.fetch("Reliance Industries Limited", "RELIANCE", now=NOW)
    q = parse_qs(urlparse(c._open.calls[0]).query)
    assert q == {"q": ['"Reliance Industries" when:7d'], "hl": ["en-IN"], "gl": ["IN"], "ceid": ["IN:en"]}
    assert r.status == "ok" and r.items[0].title == "Reliance Industries hits record" and r.raw_count == 1
    assert r.fetched_at == "2026-11-09T17:30:00+05:30"


def test_none_is_not_error_and_says_why():
    c, _ = client([feed([item("Something about Reliance Power - Mint", source="Mint")])])
    r = c.fetch("Reliance Industries Limited", "RELIANCE", now=NOW)
    assert r.status == "none" and r.dropped == {"irrelevant": 1} and "none passed the filters" in r.note
    c2, _ = client([feed([])])
    r2 = c2.fetch("Zzqx Holdings", "ZZQX", now=NOW)
    assert r2.status == "none" and r2.note == "the feed returned no items"


def test_failures_are_errors_not_empty_results():
    for script, text in (([http(503), http(503)], "HTTP 503"), ([http(403)], "HTTP 403"),
                         ([urllib.error.URLError("dns"), urllib.error.URLError("dns")], "network error"),
                         (["<html><body>consent <br> page"], "not valid XML"),
                         (["<html>consent</html>"], "no channel"),
                         (['<!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>'], "DOCTYPE"),
                         (["<rss><nochannel/></rss>"], "no channel")):
        c, _ = client(script)
        r = c.fetch("Reliance Industries", "RELIANCE", now=NOW)
        assert r.status == "error" and text in r.note and r.items == ()
    c2, _ = client([http(403), feed([])])
    c2.fetch("X Ltd", "XXX", now=NOW)
    assert len(c2._open.calls) == 1                                     # 403 is not retried
    c3, _ = client([http(503), feed([item("Reliance Industries ok - Mint", source="Mint")])])
    assert c3.fetch("Reliance Industries", "RELIANCE", now=NOW).status == "ok" and len(c3._open.calls) == 2


def test_oversized_feed_is_refused(monkeypatch):
    monkeypatch.setattr(news, "MAX_FEED_BYTES", 50)
    c, _ = client([feed([item("Reliance Industries hits record - Mint", source="Mint")])])
    r = c.fetch("Reliance Industries", "RELIANCE", now=NOW)
    assert r.status == "error" and "larger than expected" in r.note


@pytest.mark.parametrize("make_cache", [MemoryCache, lambda: RedisCache(fakeredis.FakeRedis(decode_responses=True))])
def test_cache_stores_raw_items_and_filters_are_reapplied_each_time(make_cache):
    cache = make_cache()
    c, _ = client([feed([item("Reliance Industries hits record - Mint", source="Mint")])], cache=cache)
    assert c.fetch("Reliance Industries", "RELIANCE", now=NOW).status == "ok"
    assert c.fetch("Reliance Industries", "RELIANCE", now=NOW + timedelta(hours=1)).status == "ok"
    assert len(c._open.calls) == 1                                       # second call came from the cache
    later = c.fetch("Reliance Industries", "RELIANCE", now=NOW + timedelta(days=8))
    assert later.status == "none" and later.dropped == {"old": 1}        # cached items age out
    assert len(c._open.calls) == 1
