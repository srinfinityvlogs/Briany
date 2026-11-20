# SPDX-License-Identifier: Apache-2.0
import pytest

from briany.index_samples import KEY_INDICES, ddmmyyyy, describe_csv, describe_json, main, run, run_days, split_days

CSV = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%)\r\n"
       "Nifty 50,18-11-2026,25000.1,25100.2,24900.3,25050.4,50.3,0.2\r\n"
       "Nifty Bank,18-11-2026,56000,56500,55800,56200,200,0.36\r\n"
       "India VIX,18-11-2026,12,13,11.5,12.4,0.4,3.3\r\n\r\n")
API = '{"data": [{"index": "NIFTY 50", "last": 25050.4, "percentChange": 0.2}, {"index": "NIFTY IT", "last": 40000}]}'


def test_day_format_for_the_archive_url():
    assert ddmmyyyy("2026-11-18") == "18112026"


def test_the_csv_is_read_by_header_and_key_indices_are_found_case_insensitively():
    cols, rows, found = describe_csv(CSV)
    assert cols[0] == "Index Name" and len(rows) == 3 and rows[0]["Closing Index Value"] == "25050.4"
    assert sorted(found) == ["INDIA VIX", "NIFTY 50", "NIFTY BANK"] and found["NIFTY BANK"]["Change(%)"] == "0.36"
    assert describe_csv("") == ([], [], {})


def test_the_json_is_read_from_its_data_list():
    keys, records, found = describe_json(API)
    assert keys == ["index", "last", "percentChange"] and len(records) == 2 and sorted(found) == ["NIFTY 50", "NIFTY IT"]
    assert describe_json("[]") == ([], [], {}) and describe_json('{"data": null}')[1] == []


def test_the_whole_capture_runs_offline_and_reports_each_source(tmp_path):
    served = {"ind_close_all_18112026.csv": CSV, "allIndices": API}

    def fetch(url, timeout=30):
        text = served.get(url.rsplit("/", 1)[1])
        return ({"status": 200, "seconds": 0.1, "bytes": len(text), "text": text} if text
                else {"status": 404, "seconds": 0.1, "error": "HTTPError"})
    summary = run("2026-11-18", str(tmp_path), fetch=fetch, pause=0)
    assert "index_close_csv: HTTP 200" in summary and "all_indices_api: HTTP 200" in summary
    assert "NIFTY 50: " in summary and "Closing Index Value" in summary and "percentChange" in summary
    assert (tmp_path / "summary.txt").read_text().strip() == summary and (tmp_path / "index_close_csv.csv").exists()
    assert "NIFTY 50" in KEY_INDICES


def test_a_blocked_or_garbled_source_is_reported_not_fatal(tmp_path):
    def fetch(url, timeout=30):
        if url.endswith("allIndices"):
            return {"status": 200, "seconds": 0.1, "bytes": 9, "text": "<html>no"}
        return {"status": 403, "seconds": 0.1, "error": "HTTPError"}
    summary = run("2026-11-18", str(tmp_path), fetch=fetch, pause=0)
    assert "HTTP 403" in summary and "not readable as json" in summary


def test_several_days_are_split_cleanly():
    assert split_days("2026-11-03, 2026-10-30;2026-11-03,,") == ["2026-11-03", "2026-10-30"]
    assert split_days("  ") == []


def test_a_malformed_date_stops_before_any_fetch(monkeypatch):
    for bad in ("2026-1-3", "03-11-2026", "abc", "2026-11-0x"):
        with pytest.raises(SystemExit):
            ddmmyyyy(bad)
    called = []
    monkeypatch.setattr("briany.index_samples.fetch_text", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit):
        main(["--day", "2026-11-03,2026-13"])
    assert called == []


def test_several_days_one_run_api_once_and_a_holiday_is_reported(tmp_path):
    served = {"ind_close_all_03112026.csv": CSV, "allIndices": API}
    urls = []

    def fetch(url, timeout=30):
        urls.append(url)
        text = served.get(url.rsplit("/", 1)[1])
        return ({"status": 200, "seconds": 0.1, "bytes": len(text), "text": text} if text
                else {"status": 404, "seconds": 0.1, "error": "HTTPError"})
    summary = run_days(["2026-11-03", "2026-11-10"], str(tmp_path), fetch=fetch, pause=0)
    assert sum(u.endswith("allIndices") for u in urls) == 1
    assert "2026-11-03: HTTP 200" in summary and "2026-11-10: HTTP 404" in summary
    assert (tmp_path / "2026-11-03" / "index_close_csv.csv").exists()
    assert not (tmp_path / "2026-11-10" / "index_close_csv.csv").exists()
    assert (tmp_path / "summary.txt").read_text().strip() == summary
