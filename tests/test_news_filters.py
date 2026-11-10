"""Rules added after the first real Google News capture (Nov 2026). Headlines here are INVENTED to
imitate the real patterns; real feed content is deliberately not copied into the repo."""
from datetime import datetime, timezone

import pytest

from briany import news
from briany.symbols import Instrument, Resolver

NOW = datetime(2026, 11, 9, 12, 47, tzinfo=timezone.utc)

# Same structure as a real item: <title>Headline - Source</title>, <link>, <guid isPermaLink="false">,
# <pubDate> in GMT, HTML-escaped <description>, <source url="...">; plus a namespaced element.
REAL_STRUCTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<generator>NFE/5.0</generator><title>"Acme Industries" when:7d - Google News</title><language>en-IN</language>
<lastBuildDate>Mon, 09 Nov 2026 12:47:25 GMT</lastBuildDate>
<item><title>Acme Industries Q2 profit doubles on strong demand - Example Wire</title>
<link>https://news.google.com/rss/articles/CBMiXXXX?oc=5</link><guid isPermaLink="false">CBMiXXXX</guid>
<pubDate>Tue, 03 Nov 2026 08:28:03 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiXXXX?oc=5" target="_blank"&gt;Acme Industries Q2 profit doubles&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Example Wire&lt;/font&gt;</description>
<source url="https://www.example-wire.com">Example Wire</source><media:content url="https://x/y.jpg"/></item>
</channel></rss>"""


def test_real_item_structure_parses_into_the_fields_we_use():
    raw = news.parse_feed(REAL_STRUCTURE)
    assert len(raw) == 1
    r = raw[0]
    assert r["source"] == "Example Wire" and r["source_url"] == "https://www.example-wire.com"
    assert r["link"].startswith("https://news.google.com/rss/articles/") and r["pub"].endswith("GMT")
    items, dropped = news.filter_items(raw, "Acme Industries Limited", "ACME", NOW)
    assert dropped == {} and items[0].title == "Acme Industries Q2 profit doubles on strong demand"
    assert items[0].published == "2026-11-03T13:58:03+05:30"            # GMT -> IST


# ---------------------------------------------------------------- blocklist matches exact domains and names
@pytest.mark.parametrize("source,url,blocked", [
    ("LinkedIn", "https://www.linkedin.com", True),
    ("Facebook", "", True),
    ("Screener", "https://www.screener.in", True),
    ("Simply Wall St", "https://simplywall.st", True),
    ("in.linkedin.com", "https://in.linkedin.com", True),                # subdomain of a blocked domain
    ("marketscreener.com", "https://www.marketscreener.com", False),     # the substring bug that blocked real news
    ("Moneycontrol.com", "https://www.moneycontrol.com", False),
    ("Xinhua", "https://english.news.cn", False),                        # not the source named "X"
    ("Livemint", "https://www.livemint.com", False),
    ("", "", False),
])
def test_blocked_sources(source, url, blocked):
    assert news.is_blocked_source(source, url) is blocked


# ---------------------------------------------------------------- data pages and quote pages
@pytest.mark.parametrize("title,is_page", [
    ("Acme Industries Share Price - Live NSE: ACME Stock Price & Chart - Upstox", True),
    ("ACME LTD Option Chain - Live Option Chain Data, OI & Price Analysis", True),
    ("Acme Ltd (NSE:ACME) Seasonal returns chart", True),
    ("Acme Ltd Stock Forecast & Analyst Predictions", True),
    ("Acme Ltd IPO GMP Today, Expected Listing Gain & Price Band", True),
    ("Acme Industries Q2 profit doubles on strong demand", False),
    ("Acme Industries wins Rs 400 crore order", False),
])
def test_data_page_patterns(title, is_page):
    assert bool(news.QUOTE_PAGE.search(title)) is is_page


# ---------------------------------------------------------------- ETF tickers written as two words
def test_long_tickers_match_two_word_spellings():
    tok = news.name_tokens("Nippon India ETF Gold BeES")
    assert tok == ["nippon", "etf"]
    assert news.is_relevant("Gold ETFs Today: Gold BeES, Tata Gold fall on Dhanteras", tok, "GOLDBEES")
    assert news.is_relevant("GOLDBEES sees record inflows", tok, "GOLDBEES")
    assert not news.is_relevant("Gold prices rise on Dhanteras demand", tok, "GOLDBEES")
    assert not news.is_relevant("Gold ETFs Today: Gold BeES", news.name_tokens("Krn Heat Exchanger"), "KRN")   # short symbols: word match only


# ---------------------------------------------------------------- unique first word loosens the rule, ambiguity keeps it strict
UNIVERSE = [Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD"), Instrument("RPOWER", "RELIANCE POWER LTD"),
            Instrument("TEMPSENS", "TEMPSENS INST (INDIA) LTD"),
            Instrument("TEMPSENS-RE", "TEMPSENS INST RIGHTS ENT", series="BE")]


def test_name_prefix_count_and_uniqueness_decision():
    r = Resolver(UNIVERSE)
    assert r.name_prefix_count("reliance") == 2
    assert r.name_prefix_count("TEMPSENS") == 1                  # the rights entitlement and indices are not counted
    assert r.name_prefix_count("tata") == 0
    assert news.first_token_is_unique(r, "Tempsens Instruments (India) Limited") is True
    assert news.first_token_is_unique(r, "Reliance Industries Limited") is False
    assert news.first_token_is_unique(r, "") is False


def test_unique_first_word_accepts_short_headlines_only_when_allowed():
    raw = [{"title": "Tempsens shares surge 17% on order win", "source": "Livemint", "source_url": "https://www.livemint.com",
            "link": "https://x/1", "pub": "Mon, 09 Nov 2026 05:30:00 GMT"}]
    name = "Tempsens Instruments (India) Limited"
    assert news.name_tokens(name, unique_first=True) == ["tempsens"]
    assert len(news.filter_items(raw, name, "TEMPSENS", NOW, unique_first=True)[0]) == 1
    items, dropped = news.filter_items(raw, name, "TEMPSENS", NOW)               # default stays strict
    assert items == () and dropped == {"irrelevant": 1}


def test_client_passes_unique_first_through():
    import urllib.request

    class R:
        def read(self, n=-1):
            return REAL_STRUCTURE.replace("Acme Industries Q2 profit doubles on strong demand - Example Wire",
                                          "Acme shares jump - Example Wire").encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    c = news.NewsClient(opener=lambda req, timeout=None: R(), sleep=lambda s: None, clock=lambda: 0.0)
    assert c.fetch("Acme Industries Limited", "ACME", now=NOW).status == "none"
    assert c.fetch("Acme Industries Limited", "ACME", now=NOW, unique_first=True).status == "ok"
