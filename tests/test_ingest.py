import pytest

from briany.ingest import backfill, ingest_day, redis_url_problem, weekdays
from briany.marketdata import DataError
from briany.nsefiles import FetchError
from briany.store import MemoryStore

from helpers import make_csv

PRICES = {"RELIANCE": (1311.0, 1307.9, 19451679)}


def fake_fetch(files):
    calls = []

    def fetch(day):
        calls.append(day)
        v = files.get(day)
        if isinstance(v, Exception):
            raise v
        return v
    fetch.calls = calls
    return fetch


NOSLEEP = lambda s: None  # noqa: E731


def test_ingest_day_outcomes():
    store = MemoryStore()
    fetch = fake_fetch({"2026-11-04": make_csv("2026-11-04", PRICES)})
    assert ingest_day(store, "2026-11-04", fetch=fetch) == "stored"
    assert store.get_row("2026-11-04", "RELIANCE").close == 1311.0
    assert ingest_day(store, "2026-11-04", fetch=fetch) == "exists"
    assert fetch.calls == ["2026-11-04"]                       # second run did not download
    assert ingest_day(store, "2026-11-05", fetch=fetch) == "no_file"
    assert not store.has_day("2026-11-05")
    assert ingest_day(store, "2026-11-04", fetch=fetch, force=True) == "stored"


def test_retry_then_success_and_final_failure():
    store, text = MemoryStore(), make_csv("2026-11-04", PRICES)
    state = {"n": 0}

    def flaky(day):
        state["n"] += 1
        if state["n"] == 1:
            raise FetchError("HTTP 500")
        return text
    assert ingest_day(store, "2026-11-04", fetch=flaky, sleep=NOSLEEP) == "stored"
    with pytest.raises(FetchError):
        ingest_day(MemoryStore(), "2026-11-04", fetch=fake_fetch({"2026-11-04": FetchError("HTTP 403")}),
                   sleep=NOSLEEP)


def test_refuses_suspiciously_small_or_wrong_files():
    with pytest.raises(DataError):
        ingest_day(MemoryStore(), "2026-11-04",
                   fetch=fake_fetch({"2026-11-04": make_csv("2026-11-04", PRICES, pad=10)}))
    with pytest.raises(DataError):
        ingest_day(MemoryStore(), "2026-11-04",
                   fetch=fake_fetch({"2026-11-04": make_csv("2026-11-03", PRICES)}))


def test_weekdays_skips_weekends():
    assert list(weekdays("2026-11-06", "2026-11-10")) == ["2026-11-06", "2026-11-09", "2026-11-10"]


def test_backfill_counts_and_polite_sleeps():
    store, slept, logs = MemoryStore(), [], []
    files = {"2026-11-06": make_csv("2026-11-06", PRICES),
             "2026-11-09": None,                                   # holiday
             "2026-11-10": make_csv("2026-11-10", PRICES)}
    store.put_day("2026-11-10", {"X": None})                       # already stored
    counts = backfill(store, "2026-11-06", "2026-11-10", fetch=fake_fetch(files),
                      min_interval=1.0, sleep=slept.append, log=logs.append)
    assert dict(counts) == {"stored": 1, "no_file": 1, "exists": 1}
    assert slept == [1.0, 1.0]                                     # no sleep for skipped day
    assert len(logs) == 3


def test_backfill_stops_on_block():
    files = {"2026-11-06": FetchError("HTTP 403 for 2026-11-06")}
    with pytest.raises(FetchError):
        backfill(MemoryStore(), "2026-11-06", "2026-11-10", fetch=fake_fetch(files),
                 sleep=NOSLEEP, log=lambda *_: None)


def test_redis_url_problem_hints_never_echo_the_value():
    assert redis_url_problem("rediss://default:pw@host:6379") is None
    assert redis_url_problem("redis://localhost:6379") is None
    cases = {
        "https://abc.upstash.io": "REST",
        "redis-cli --tls -u rediss://default:SECRETPW@h:6379": "spaces",
        '"rediss://default:SECRETPW@h:6379"': "quote",
        "REDIS_URL=rediss://default:SECRETPW@h:6379": "NAME=",
        "SECRETPW@host.upstash.io": "host and a password",
    }
    for value, word in cases.items():
        msg = redis_url_problem(value)
        assert word in msg and "SECRETPW" not in msg and "upstash" not in msg
