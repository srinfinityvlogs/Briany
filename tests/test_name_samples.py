# SPDX-License-Identifier: Apache-2.0
from briany.name_samples import KEY_SYMBOLS, coverage, read_names, run
from briany.symbols import Instrument
from helpers import make_csv

EQUITY = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
          "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
          "TCS,Tata Consultancy  Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1\n"
          "BLANK,,EQ,01-JAN-2000,10,1,INE000000000,10\n")
ETF = "\ufeffSymbol,Underlying,SecurityName,DateofListing\nGOLDBEES,Gold,Some ETF Name,08-MAR-2007\n"


def test_names_are_read_by_header_even_with_spaces_bom_and_double_spaces():
    cols, sym, name, names = read_names(EQUITY)
    assert sym == "SYMBOL" and name == "NAME OF COMPANY" and "SERIES" in cols       # header whitespace is stripped
    assert names == {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy Services Limited"}
    _c, sym, name, names = read_names(ETF)
    assert (sym, name, names) == ("Symbol", "SecurityName", {"GOLDBEES": "Some ETF Name"})


def test_a_file_without_recognisable_columns_is_reported_not_guessed():
    cols, sym, name, names = read_names("A,B\n1,2\n")
    assert cols == ["A", "B"] and sym is None and name is None and names == {}
    assert read_names("")[3] == {}


def test_coverage_counts_what_each_source_names_and_lists_the_rest():
    inst = [Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD"), Instrument("TCS", "TATA CONSULTANCY SERV LT"),
            Instrument("GOLDBEES", "NIP IND ETF GOLD BEES", turnover=5.0), Instrument("ZZZ", "ZZZ LTD", series="SM")]
    cov = coverage(inst, {"equity_list": {"RELIANCE": "x", "TCS": "y"}, "etf_list": {"GOLDBEES": "z"}})
    assert cov["total"] == 4 and cov["named_by_any"] == 3 and cov["by_source"] == {"equity_list": 2, "etf_list": 1}
    assert [i.symbol for i in cov["missing"]] == ["ZZZ"]


def test_the_whole_capture_runs_offline_and_writes_a_readable_summary(tmp_path):
    served = {"EQUITY_L.csv": EQUITY, "eq_etfseclist.csv": ETF}

    def fetch(url, timeout=30):
        for end, text in served.items():
            if url.rsplit("/", 1)[1] == end:
                return {"status": 200, "seconds": 0.1, "bytes": len(text), "text": text}
        return {"status": 404, "seconds": 0.1, "error": "HTTPError"}

    prices = make_csv("2026-11-18", {"RELIANCE": (1363.0, 1367.0, 10), "GOLDBEES": (107.85, 108.0, 10),
                                     "TCS": (2379.5, 2380.0, 10), "NOPE": (10.0, 10.0, 10)}, pad=0)
    summary = run("2026-11-18", str(tmp_path), fetch=fetch, bhavcopy=lambda day: prices, pause=0)
    assert "equity_list: HTTP 200" in summary and "sme_list: HTTP 404" in summary
    assert "RELIANCE: Reliance Industries Limited" in summary and "GOLDBEES: Some ETF Name" in summary
    assert "named by at least one source: 3" in summary and "NOT named by any source: 1" in summary and "NOPE" in summary
    assert (tmp_path / "summary.txt").read_text().strip() == summary and (tmp_path / "equity_list.csv").exists()
    assert set(KEY_SYMBOLS) >= {"RELIANCE", "GOLDBEES"}


def test_a_day_without_a_price_file_still_produces_a_summary(tmp_path):
    summary = run("2026-11-10", str(tmp_path), fetch=lambda url, timeout=30: {"status": 404, "seconds": 0, "error": "HTTPError"},
                  bhavcopy=lambda day: None, pause=0)
    assert "not published" in summary and "coverage not measured" in summary
