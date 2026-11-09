"""Company news headlines from Google News RSS. Stdlib only.

Keeps headline, source, time and link. No article text, and no claim that a headline
explains a price move. Outcomes: ok / none ("no news found") / error ("could not look").

Built on the URL pattern Phase 1 verified; the feed layout (RSS 2.0 items with title,
link, pubDate and <source>) is UNVERIFIED against a real response until news_samples runs.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
RSS_URL = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/rss+xml, application/xml, text/xml, */*"}
MAX_FEED_BYTES = 2_000_000
TTL_NEWS = 900

GENERIC = frozenset({"limited", "ltd", "pvt", "private", "the", "co", "company", "corp", "corporation",
                     "india", "indian", "of", "and", "&"})
# Phase 1: small-cap queries returned quote pages ("... Share Price Live ...") instead of news.
QUOTE_PAGE = re.compile(
    r"(share|stock) price|\blive (price|chart|quote|stock)|\b(stock|share) quote\b|\bhistorical (data|prices?)\b|"
    r"\bprice today\b|\bprice,? (chart|news)\b|\bfinancials?,? (ratios?|results?) &|\bpe ratio\b", re.I)
BLOCKED_SOURCES = ("screener", "trendlyne", "tickertape")        # quote/data sites (unverified list)


class NewsError(RuntimeError):
    pass


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published: str          # ISO datetime in IST, e.g. 2026-11-09T11:00:00+05:30
    url: str


@dataclass(frozen=True)
class NewsResult:
    status: str             # "ok" | "none" | "error"
    query: str
    items: tuple = ()
    fetched_at: str = ""
    raw_count: int = 0
    dropped: dict = field(default_factory=dict)      # reason -> count
    note: str = ""


# ------------------------------------------------------------------ query and relevance
def clean_company_name(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", name or "")
    words = [w for w in s.split() if w.lower().strip(".,") not in ("limited", "ltd", "pvt", "private")]
    return " ".join(words).strip()


def build_query(name: str, days: int = 7) -> str:
    return f'"{clean_company_name(name)}" when:{days}d'


def name_tokens(name: str):
    """Up to two distinctive words of the company name that a relevant headline must contain."""
    s = re.sub(r"\([^)]*\)", " ", name or "")
    toks = [t for t in re.findall(r"[a-z0-9&]+", s.lower()) if t not in GENERIC and len(t) >= 2]
    return toks[:2]


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def is_relevant(title: str, tokens, symbol: str = "") -> bool:
    t = title.lower()
    if tokens and all(_has_word(t, w) for w in tokens):
        return True
    # A ticker alone is enough only when it is not itself a name word: RELIANCE must not match
    # "Reliance Power", while TCS may match "TCS wins deal" for Tata Consultancy Services.
    sym = (symbol or "").lower()
    return len(sym) >= 3 and sym not in tokens and _has_word(t, sym)


def _title_key(title: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
    return " ".join(words[:10])


# ------------------------------------------------------------------ feed parsing
def parse_feed(text: str):
    """RSS text -> list of raw dicts {title, source, source_url, link, pub}. Raises NewsError."""
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise NewsError("feed declares a DOCTYPE or entities; refusing to parse")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise NewsError("feed is not valid XML (possibly a consent or error page)") from None
    channel = root.find("channel")
    if channel is None:
        raise NewsError("feed has no channel element")
    out = []
    for it in channel.findall("item"):
        src = it.find("source")
        out.append({"title": (it.findtext("title") or "").strip(),
                    "source": (src.text or "").strip() if src is not None else "",
                    "source_url": (src.get("url") or "") if src is not None else "",
                    "link": (it.findtext("link") or "").strip(),
                    "pub": (it.findtext("pubDate") or "").strip()})
    return out


def _parse_pub(text: str):
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def filter_items(raw, name: str, symbol: str, now: datetime, days: int = 7, limit: int = 5):
    """-> (items newest first, dropped-reason counts)."""
    tokens, dropped, kept = name_tokens(name), {}, []

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for r in raw:
        when = _parse_pub(r.get("pub", ""))
        if when is None:
            drop("bad_date"); continue
        if when < now - timedelta(days=days):
            drop("old"); continue
        if when > now + timedelta(days=1):
            drop("future"); continue
        title, source = r.get("title", ""), r.get("source", "")
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        hay = (source + " " + r.get("source_url", "")).lower()
        if any(b in hay for b in BLOCKED_SOURCES):
            drop("blocked_source"); continue
        if not title or not r.get("link"):
            drop("incomplete"); continue
        if QUOTE_PAGE.search(title):
            drop("quote_page"); continue
        if not is_relevant(title, tokens, symbol):
            drop("irrelevant"); continue
        kept.append((when, title, source, r["link"]))
    kept.sort(key=lambda x: x[0], reverse=True)
    items, seen = [], set()
    for when, title, source, link in kept:
        key = _title_key(title)
        if key in seen:
            drop("duplicate"); continue
        seen.add(key)
        items.append(NewsItem(title, source, when.astimezone(IST).isoformat(timespec="seconds"), link))
    return tuple(items[:limit]), dropped


# ------------------------------------------------------------------ client
class NewsClient:
    def __init__(self, cache=None, min_interval=1.0, opener=None, sleep=time.sleep,
                 clock=time.monotonic, retries=1, timeout=20):
        self.cache, self.min_interval = cache, min_interval
        self._open = opener or urllib.request.urlopen
        self._sleep, self._clock, self.retries, self.timeout = sleep, clock, retries, timeout
        self._last = None

    def _throttle(self):
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _download(self, query: str) -> str:
        url = RSS_URL + "?" + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with self._open(urllib.request.Request(url, headers=HEADERS), timeout=self.timeout) as r:
                    body = r.read(MAX_FEED_BYTES + 1)
                break
            except urllib.error.HTTPError as e:
                if e.code >= 500 and attempt < self.retries:
                    self._sleep(2)
                    continue
                raise NewsError(f"HTTP {e.code} from Google News") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.retries:
                    self._sleep(2)
                    continue
                raise NewsError(f"network error ({type(e).__name__}) from Google News") from None
        if len(body) > MAX_FEED_BYTES:
            raise NewsError("feed larger than expected; refusing to parse")
        return body.decode("utf-8", "replace")

    def fetch(self, name: str, symbol: str = "", now: datetime | None = None, days: int = 7, limit: int = 5):
        now = now or datetime.now(timezone.utc)
        query = build_query(name or symbol, days)
        stamp = now.astimezone(IST).isoformat(timespec="seconds")
        key = "news:" + query
        raw = self.cache.get(key) if self.cache is not None else None
        if raw is None:
            try:
                raw = parse_feed(self._download(query))
            except NewsError as e:
                return NewsResult("error", query, (), stamp, 0, {}, str(e))
            if self.cache is not None:
                self.cache.set(key, raw, TTL_NEWS)
        items, dropped = filter_items(raw, name or symbol, symbol, now, days, limit)
        if items:
            return NewsResult("ok", query, items, stamp, len(raw), dropped)
        note = ("the feed returned no items" if not raw
                else f"{len(raw)} item(s) found but none passed the filters")
        return NewsResult("none", query, (), stamp, len(raw), dropped, note)
