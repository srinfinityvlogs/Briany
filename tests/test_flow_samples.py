# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from briany.flow_samples import candidates, describe, nse_date, run
from datetime import date

FIX = Path(__file__).parent / "fixtures"
BULK = json.dumps(json.loads((FIX / "bulk_snapshot.json").read_text())["sample"])
FII = json.dumps(json.loads((FIX / "fii_dii.json").read_text())["sample"])


def _row(i):
    return {"buySell": "BUY", "clientName": f"C{i}", "date": "20-Nov-2026", "name": "N", "qty": "5", "symbol": "S", "watp": "9"}


def test_declared_counts_are_compared_with_the_rows_actually_listed():
    data = {"as_on_date": "20-Nov-2026", "BULK_DEALS": "3", "BULK_DEALS_DATA": [_row(i) for i in range(3)],
            "SHORT_DEALS": "5", "SHORT_DEALS_DATA": [_row(0), _row(1)], "BLOCK_DEALS": "1", "BLOCK_DEALS_DATA": [_row(9)]}
    lines = "\n".join(describe(json.dumps(data)))
    assert "BULK_DEALS: declared 3, listed 3 (complete)" in lines and "BLOCK_DEALS: declared 1, listed 1 (complete)" in lines
    assert "SHORT_DEALS: declared 5, listed 2 (INCOMPLETE: 3 missing)" in lines
    assert "list BULK_DEALS_DATA: 3 rows" in lines and "rows per day 20-Nov-2026=3" in lines


def test_the_saved_fixture_was_cut_by_the_sampler_so_it_cannot_prove_completeness():
    sample = json.loads(BULK)
    assert str(sample["BULK_DEALS_DATA"][-1]).startswith("...[71 more items]")      # 25 + 71 = the declared 96
    assert 25 + 71 == int(sample["BULK_DEALS"]) and 25 + 117 == int(sample["SHORT_DEALS"]) and len(sample["BLOCK_DEALS_DATA"]) == 20


def test_fii_dii_sample_has_two_categories_for_one_day():
    lines = "\n".join(describe(FII))
    assert "list $: 2 rows" in lines and "date: 1 distinct days; rows per day 06-Nov-2026=2" in lines


def test_describe_html_empty_csv_and_plain_json():
    assert describe("<!DOCTYPE html><title>Service Temporarily Unavailable</title>")[0].startswith("HTML (an error")
    assert describe("") == ["empty"]
    assert describe("{not json") == ["looks like JSON but does not parse"]
    csv_lines = describe("SYMBOL,Date,Qty\nAAA,20-Nov-2026,5\nBBB,20-Nov-2026,7\nCCC,19-Nov-2026,9\n")
    assert csv_lines[0].startswith("CSV: 3 rows") and "2 distinct days" in csv_lines[1] and "20-Nov-2026=2" in csv_lines[1]
    assert "no list of records" in " ".join(describe('{"a": 1}'))


def test_windows_end_on_the_requested_day():
    c = {n: u for n, u, _ in candidates(date(2026, 11, 20))}
    assert nse_date(date(2026, 11, 3)) == "03-11-2026"
    assert "from=13-11-2026&to=20-11-2026" in c["hist_bulk_week"] and "optionType=block_deals" in c["hist_block_week"]
    assert "from=22-08-2026&to=20-11-2026" in c["hist_bulk_3m"]


def test_run_writes_a_summary_even_when_candidates_fail(tmp_path):
    def fetcher(url, timeout=40):
        if url.endswith("fiidiiTradeReact"):
            return {"status": 200, "seconds": 0.1, "bytes": len(FII), "text": FII}
        if "bulk.csv" in url:
            return {"status": 200, "seconds": 0.1, "bytes": 40, "text": "<html>Service Temporarily Unavailable</html>"}
        return {"status": 404, "seconds": 0.1, "error": "HTTPError"}
    text = run("2026-11-20", str(tmp_path), fetcher=fetcher, pause=0)
    assert (tmp_path / "summary.txt").read_text().strip() == text
    assert text.count("== ") == 10 and "UNVERIFIED candidate" in text.splitlines()[0]
    assert "fii_dii_api (known: latest day only): HTTP 200" in text and "list $: 2 rows" in text
    assert "bulk_csv (candidate archive file): HTTP 200" in text and "HTML (an error" in text
    assert "hist_bulk_3m (candidate: how deep, how big): HTTP 404" in text and "failed: HTTPError" in text
    assert (tmp_path / "fii_dii_api.json").exists() and not (tmp_path / "hist_bulk_3m.json").exists()
    with pytest.raises(SystemExit):
        run("20-11-2026", str(tmp_path), fetcher=fetcher, pause=0)
