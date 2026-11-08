from briany.crosscheck import crosscheck
from briany.marketdata import PriceRow
from briany.nse_parsers import CorpAction
from briany.store import MemoryStore


def act(sym, ex, kind, blocking=True):
    return CorpAction(sym, sym, ex, None, kind, kind, blocking)


def test_crosscheck_separates_explained_and_unexplained_moves():
    s = MemoryStore()
    s.put_day("2026-01-02", {"SPLITCO": PriceRow(1000.0), "MOVER": PriceRow(100.0),
                             "BONUSMILD": PriceRow(100.0), "DIVSTOCK": PriceRow(100.0), "FLAT": PriceRow(50.0)})
    s.put_day("2026-01-05", {"SPLITCO": PriceRow(100.0), "MOVER": PriceRow(60.0),
                             "BONUSMILD": PriceRow(80.0), "DIVSTOCK": PriceRow(50.0), "FLAT": PriceRow(50.5)})
    actions = [act("SPLITCO", "2026-01-05", "split"), act("BONUSMILD", "2026-01-05", "bonus"),
               act("DIVSTOCK", "2026-01-05", "dividend", blocking=False),
               act("OLD", "2025-06-01", "split")]                       # outside the stored range
    lines = []
    res = crosscheck(s, actions, out=lines.append)
    assert (res["total"], res["matched"], res["unmatched"]) == (3, 1, 2)
    assert res["visible"] == {"drop >= 30%": 1, "drop 10-30%": 1}
    text = "\n".join(lines)
    assert "explained by a blocking corporate action" in text and "split 1" in text
    assert "2026-01-05 MOVER: 100.0 -> 60.0 (-40.0%)" in text
    assert "2026-01-05 DIVSTOCK: 100.0 -> 50.0 (-50.0%)" in text      # a dividend does not explain -50%
    assert "SPLITCO: 1000.0" not in text.split("largest unexplained")[1]


def test_crosscheck_handles_empty_store():
    res = crosscheck(MemoryStore(), [], out=lambda *_: None)
    assert res["total"] == 0 and res["visible"] == {}
