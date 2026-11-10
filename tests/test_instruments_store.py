import fakeredis
import pytest

from briany.ingest import ingest_day
from briany.store import MemoryStore, RedisStore, pack_instruments
from briany.symbols import Instrument

from helpers import make_csv


@pytest.fixture(params=["memory", "redis"])
def store(request):
    return MemoryStore() if request.param == "memory" else RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_instrument_list_roundtrip(store):
    assert store.get_instruments() == (None, [])
    items = [Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD", "INE002A01018", "EQ", 2.5e10),
             Instrument("ODD", "NAME | WITH PIPE", "", "BE", 0.0)]
    store.set_instruments("2026-11-09", items)
    day, got = store.get_instruments()
    assert day == "2026-11-09" and [(i.symbol, i.name, i.isin, i.series) for i in got] == [
        ("RELIANCE", "RELIANCE INDUSTRIES LTD", "INE002A01018", "EQ"), ("ODD", "NAME / WITH PIPE", "", "BE")]
    assert got[0].turnover == 2.5e10
    store.set_instruments("2026-11-10", items[:1])
    assert store.get_instruments()[0] == "2026-11-10" and len(store.get_instruments()[1]) == 1


def test_instrument_list_stays_small():
    import random
    random.seed(3)
    items = [Instrument(f"SYM{i:04d}", f"COMPANY NUMBER {i} INDUSTRIES LTD", f"INE{i:09d}01", "EQ", random.random() * 1e9)
             for i in range(3443)]
    assert len(pack_instruments(items)) < 150_000


def test_ingest_stores_names_only_for_the_newest_day():
    s = MemoryStore()
    for day in ("2026-11-04", "2026-11-03"):                          # newest first, then an older backfill
        text = make_csv(day, {"RELIANCE": (1311.0, 1307.9, 5)})
        assert ingest_day(s, day, fetch=lambda d, t=text: t) == "stored"
    day, items = s.get_instruments()
    assert day == "2026-11-04" and "RELIANCE" in {i.symbol for i in items} and len(items) >= 600
