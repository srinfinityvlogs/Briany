import fakeredis
import pytest

from briany.marketdata import PriceRow
from briany.store import MemoryStore, RedisStore, decode, encode


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    return RedisStore(fakeredis.FakeRedis(decode_responses=True), chunk=1000)


def test_roundtrip_and_navigation(store):
    store.put_day("2026-11-03", {"A": PriceRow(10.5, 10.0, 100)})
    store.put_day("2026-11-05", {"A": PriceRow(11.0, 10.5, None), "B": PriceRow(5.0)})
    assert store.has_day("2026-11-03") and not store.has_day("2026-11-04")
    assert store.get_row("2026-11-05", "A") == PriceRow(11.0, 10.5, None)
    assert store.get_row("2026-11-05", "B") == PriceRow(5.0, None, None)
    assert store.get_row("2026-11-05", "ZZZ") is None
    assert store.get_row("2026-11-04", "A") is None
    assert store.day_on_or_before("2026-11-04") == "2026-11-03"
    assert store.day_on_or_before("2026-11-09") == "2026-11-05"
    assert store.day_on_or_before("2026-11-02") is None
    assert (store.earliest_day(), store.latest_day()) == ("2026-11-03", "2026-11-05")


def test_empty_store(store):
    assert store.latest_day() is None and store.earliest_day() is None
    assert store.day_on_or_before("2026-11-04") is None


def test_rewriting_a_day_replaces_it(store):
    store.put_day("2026-11-03", {"A": PriceRow(1.0), "OLD": PriceRow(2.0)})
    store.put_day("2026-11-03", {"A": PriceRow(3.0)})
    assert store.get_row("2026-11-03", "A").close == 3.0
    assert store.get_row("2026-11-03", "OLD") is None


def test_large_day_is_chunked(store):
    rows = {f"S{i:05d}": PriceRow(100.0 + i, 99.0 + i, i) for i in range(3500)}
    store.put_day("2026-11-04", rows)
    assert store.get_row("2026-11-04", "S03499") == rows["S03499"]
    assert store.get_row("2026-11-04", "S00000") == rows["S00000"]


def test_encode_decode_keeps_exact_values():
    for row in (PriceRow(1311.0, 1307.9, 19451679), PriceRow(0.05), PriceRow(7.0, None, 3)):
        assert decode(encode(row)) == row
