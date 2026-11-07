import fakeredis
import pytest

from briany.marketdata import PriceRow
from briany.store import MemoryStore, RedisStore, decode, encode, pack_day, purge_legacy, unpack_day


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


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


def test_large_day_roundtrips(store):
    rows = {f"S{i:05d}": PriceRow(100.0 + i, 99.0 + i, i) for i in range(3500)}
    store.put_day("2026-11-04", rows)
    assert store.get_row("2026-11-04", "S03499") == rows["S03499"]
    assert store.get_row("2026-11-04", "S00000") == rows["S00000"]


def test_encode_decode_keeps_exact_values():
    for row in (PriceRow(1311.0, 1307.9, 19451679), PriceRow(0.05), PriceRow(7.0, None, 3)):
        assert decode(encode(row)) == row


def test_packed_day_is_compact_and_lossless():
    import random
    random.seed(7)
    rows = {}
    for i in range(3443):
        c = round(random.uniform(5, 5000), 2)
        rows[f"SYM{i:04d}"] = PriceRow(c, round(c * random.uniform(0.97, 1.03), 2), random.randint(100, 9_000_000))
    blob = pack_day(rows)
    assert unpack_day(blob) == rows
    assert len(blob) < 110_000          # per day; the old hash layout needed several times this


def test_purge_legacy_removes_only_old_px_keys():
    r = fakeredis.FakeRedis(decode_responses=True)
    for i in range(250):
        r.hset(f"px:2026-01-{i:03d}", mapping={"A": "1|1|1"})
    r.zadd("px:days", {"2026-01-01": 20260101})
    r.set("pxz:2026-01-02", "keep-me")
    r.zadd("pxz:days", {"2026-01-02": 20260102})
    r.set("spike:ping", "keep-me-too")
    assert purge_legacy(r) == 251
    assert sorted(r.keys("*")) == ["pxz:2026-01-02", "pxz:days", "spike:ping"]
    assert purge_legacy(r) == 0


def test_all_days_and_get_day(store):
    store.put_day("2026-11-05", {"B": PriceRow(2.0)})
    store.put_day("2026-11-03", {"A": PriceRow(1.0, 1.0, 5)})
    assert store.all_days() == ["2026-11-03", "2026-11-05"]
    assert store.get_day("2026-11-03") == {"A": PriceRow(1.0, 1.0, 5)}
    assert store.get_day("2026-11-04") == {}


def test_prune_before_removes_only_older_days_with_cap(store):
    for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"):
        store.put_day(d, {"A": PriceRow(1.0)})
    assert store.prune_before("2026-01-07", max_days=1) == ["2026-01-05"]      # cap respected
    assert store.prune_before("2026-01-07") == ["2026-01-06"]                  # strictly before
    assert store.prune_before("2026-01-07") == []                              # idempotent
    assert store.all_days() == ["2026-01-07", "2026-01-08"]
    assert store.get_row("2026-01-05", "A") is None and not store.has_day("2026-01-06")
    assert store.get_row("2026-01-07", "A") == PriceRow(1.0)
