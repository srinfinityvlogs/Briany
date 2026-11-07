from briany.dbcheck import KNOWN_CLOSES, chain_breaks, run_check
from briany.marketdata import PriceRow
from briany.store import MemoryStore


def store_with_known(adjusted_symbol=False):
    s = MemoryStore()
    prev = None
    for day, close in KNOWN_CLOSES["RELIANCE"].items():
        rows = {"RELIANCE": PriceRow(close, prev if prev else close, 1),
                "SPLIT": PriceRow(100.0 if day < "2026-11-06" else 52.0,
                                  100.0 if day < "2026-11-06" else (50.0 if adjusted_symbol else 100.0), 1)}
        s.put_day(day, rows)
        prev = close
    return s


def test_chain_breaks_finds_adjusted_previous_close():
    s = store_with_known(adjusted_symbol=True)
    assert chain_breaks(s, "2026-11-06", "2026-11-05") == [("SPLIT", 100.0, 50.0)]
    assert chain_breaks(s, "2026-11-05", "2026-11-04") == []


def test_run_check_passes_on_known_data_and_reports():
    lines = []
    assert run_check(store_with_known(), out=lines.append) is True
    text = "\n".join(lines)
    assert "stored days: 4" in text and "CHECK PASSED" in text
    assert "RELIANCE daily move: ok 2026-11-06" in text


def test_run_check_fails_on_wrong_close_or_empty_store():
    s = store_with_known()
    s.put_day("2026-11-04", {"RELIANCE": PriceRow(1000.0, 1307.9, 1)})
    assert run_check(s, out=lambda *_: None) is False
    assert run_check(MemoryStore(), out=lambda *_: None) is False


def test_run_check_reports_weekend_sessions_and_chain_audit():
    s = store_with_known(adjusted_symbol=True)
    s.put_day("2026-11-08", {"RELIANCE": PriceRow(1321.0, 1320.0, 1)})   # a Sunday (Muhurat)
    lines = []
    run_check(s, out=lines.append)
    text = "\n".join(lines)
    assert "weekend sessions stored (1): 2026-11-08" in text
    assert "chain audit over 4 day pairs: 1 day(s) with breaks" in text
    assert "2026-11-06: 1" in text
