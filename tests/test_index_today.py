# SPDX-License-Identifier: Apache-2.0
"""Which index close does 'today' mean, and how long is a fetched file kept?
Cache: today's and yesterday's file 1 hour, anything older 7 days, failures never. The note 'No close for today is stored yet'
appears on a weekday after the 09:15 open when the newest stored close is from an earlier day; never before 09:15, never at the
weekend, never when the user named the day."""
from datetime import datetime

import pytest

from briany.answer import fallback_text, premise_note, validate_text
from briany.bot import Bot
from briany.evidence import IST, Assembler
from briany.indexfile import TTL_PAST, TTL_RECENT, IndexClient, IndexFileError
from briany.nse import MemoryCache
from test_evidence import FakeNews, FakeNse
from test_index_close import NOTHING, RESOLVER, DeadChain, Opener, store


class SpyCache(MemoryCache):
    def __init__(self):
        super().__init__()
        self.ttls = {}

    def set(self, key, value, ttl):
        self.ttls[key] = ttl
        return super().set(key, value, ttl)


def at(y, m, d, hh=11, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# ================================================================ cache lifetime
@pytest.mark.parametrize("now,expected", [
    (at(2026, 11, 3, 20), TTL_RECENT),            # the file's own day
    (at(2026, 11, 4, 9), TTL_RECENT),             # the next morning: still yesterday
    (at(2026, 11, 4, 23), TTL_RECENT),            # late the next night: still yesterday (age 1)
    (at(2026, 11, 5, 0), TTL_PAST),               # two days on: final for good
    (at(2026, 11, 10, 12), TTL_PAST),
])
def test_a_file_is_kept_an_hour_when_recent_and_a_week_when_older(now, expected):
    cache = SpyCache()
    IndexClient(cache=cache, opener=Opener(), sleep=lambda s: None, now=lambda: now).day("2026-11-03")
    assert cache.ttls == {"idx:2026-11-03": expected}
    assert (TTL_RECENT, TTL_PAST) == (3600, 7 * 86400)


def test_a_fixed_lifetime_can_still_be_forced_and_failures_are_never_cached():
    cache = SpyCache()
    IndexClient(cache=cache, opener=Opener(), sleep=lambda s: None, ttl=60, now=lambda: at(2026, 11, 10)).day("2026-11-03")
    assert cache.ttls == {"idx:2026-11-03": 60}
    failing = SpyCache()
    with pytest.raises(IndexFileError):
        IndexClient(cache=failing, opener=Opener(NOTHING), sleep=lambda s: None, now=lambda: at(2026, 11, 10)).day("2026-11-03")
    assert failing.ttls == {}


def test_a_cached_older_day_is_not_fetched_again():
    cache, op = SpyCache(), Opener()
    c = IndexClient(cache=cache, opener=op, sleep=lambda s: None, now=lambda: at(2026, 11, 10))
    c.day("2026-11-03")
    c.day("2026-11-03")
    assert len(op.urls) == 1


def test_a_cached_day_stays_a_week_in_memory_cache_terms_and_is_small():
    cache = SpyCache()
    IndexClient(cache=cache, opener=Opener(), sleep=lambda s: None, now=lambda: at(2026, 11, 10)).day("2026-11-03")
    import json
    assert len(json.dumps(cache.get("idx:2026-11-03"))) < 25_000           # about 17 KB a day, so a week of distinct days is tiny


# ================================================================ the note about today
def asm(now, days=("2026-10-30", "2026-11-03"), opener=None):
    client = IndexClient(cache=MemoryCache(), opener=opener or Opener(), sleep=lambda s: None, now=lambda: now)
    return Assembler(store(*days), FakeNse(), FakeNews(), RESOLVER, now=lambda: now, index=client)


NOTE = "No close for today is stored yet"


@pytest.mark.parametrize("now,shown", [
    (at(2026, 11, 4, 9, 14), False),     # Tuesday just before the open: the earlier close is simply the latest
    (at(2026, 11, 4, 9, 15), True),      # the open
    (at(2026, 11, 4, 11), True),         # session in progress
    (at(2026, 11, 4, 17), True),         # after the close, file not stored yet
    (at(2026, 11, 3, 20), False),        # the stored day IS today
    (at(2026, 11, 7, 11), False),        # Saturday: the latest close is Tuesday's... and nothing is expected today
    (at(2026, 11, 8, 11), False),        # Sunday
])
def test_the_note_follows_the_clock_exactly(now, shown):
    p = asm(now).index_close("nifty")
    assert p.status == "ok" and p.facts[0].date == "2026-11-03"
    assert (NOTE in fallback_text(p)) is shown
    assert ("no_close_today" in [g.code for g in p.gaps]) is shown


def test_the_note_text_and_that_the_validator_accepts_it():
    p = asm(at(2026, 11, 4, 11)).index_close("nifty")
    text = fallback_text(p)
    assert ("No close for today is stored yet (the market may still be open, closed for a holiday, or NSE's file may not be out "
            "yet), so this is the last stored close, 3 Nov 2026.") in text
    assert text.startswith("Nifty 50 closed at 23,810.85 on 3 Nov 2026") and validate_text(text, p) == []


def test_a_named_day_never_gets_the_note():
    p = asm(at(2026, 11, 4, 11)).index_close("nifty", on="2026-10-30")
    assert all(g.code != "no_close_today" for g in p.gaps) and NOTE not in fallback_text(p)


def test_a_fallback_day_gets_the_fallback_label_instead_of_the_today_note():
    a = asm(at(2026, 11, 10, 20), days=("2026-10-30", "2026-11-03", "2026-11-10"))
    p = a.index_close("nifty")
    codes = [g.code for g in p.gaps]
    assert "index_latest_missing" in codes and "no_close_today" not in codes


def test_the_fallback_only_goes_back_one_stored_day():
    only_old = {"30102026": Opener().files["30102026"]}                      # 3 Nov and 10 Nov missing; 30 Oct present
    p = asm(at(2026, 11, 10, 20), days=("2026-10-30", "2026-11-03", "2026-11-10"), opener=Opener(only_old)).index_close("nifty")
    assert p.status == "refused" and p.gaps[0].code == "index_file_unavailable" and "10 Nov 2026" in p.gaps[0].text


# ================================================================ the question people actually ask
def bot(now):
    return Bot(asm(now), DeadChain(), RESOLVER, now=lambda: now)


def test_why_is_the_nifty_down_before_and_after_the_open():
    before = bot(at(2026, 11, 4, 8, 30)).ask("why is the nifty down today?")
    assert before.text.startswith("Nifty 50 closed at 23,810.85 on 3 Nov 2026") and NOTE not in before.text
    assert "I can show where an index closed, but I don't explain index moves." in before.text
    after = bot(at(2026, 11, 4, 11)).ask("why is the nifty down today?")
    assert after.text.startswith("Nifty 50 closed at 23,810.85 on 3 Nov 2026") and NOTE in after.text
    assert "I can show where an index closed, but I don't explain index moves." in after.text
    assert "which is not a decrease" in after.text                              # the 3 Nov change was up, not down
    assert validate_text(after.body, after.packet) == []
