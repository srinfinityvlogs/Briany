# SPDX-License-Identifier: Apache-2.0
import fakeredis
import pytest

from briany.evidence import Assembler
from briany.names import MIN_EQUITY_NAMES, NamesError, build_names, read_names, refresh_names
from briany.refresh_names import main as refresh_main
from briany.store import MemoryStore, RedisStore, pack_names, unpack_names
from test_evidence import NOW, RESOLVER, FakeNews, FakeNse, ann, build_store, returns_store

HEADER = "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"


def equity_text(extra=0, drop=()):
    rows = {"RELIANCE": "Reliance Industries Limited", "TCS": "Tata Consultancy  Services Limited",
            "INFY": "Infosys Limited", "ACME": "Acme Full Name Limited"}
    rows.update({f"S{i}": f"Company {i} Limited" for i in range(extra)})
    return HEADER + "".join(f"{s},{n},EQ,01-JAN-2000,10,1,INE000A01000,10\n" for s, n in rows.items() if s not in drop)


BIG = equity_text(extra=MIN_EQUITY_NAMES)
SME = HEADER + "THEJO,Thejo Engineering Limited,SM,18-SEP-2012,10,300,INE121N01019,10\r\n\r\n**This file is updated daily\n"


def fake_fetch(equity=BIG, sme=SME):
    def fetch(url, timeout=30):
        text = equity if "SME" not in url else sme
        if isinstance(text, Exception):
            raise text
        return text
    return fetch


# ---------------------------------------------------------------- parsing and checks
def test_equity_names_are_parsed_and_tidied():
    _c, sym, name, names = read_names(equity_text())
    assert (sym, name) == ("SYMBOL", "NAME OF COMPANY") and names["TCS"] == "Tata Consultancy Services Limited"


def test_a_broken_main_list_is_rejected_with_a_reason():
    with pytest.raises(NamesError, match="no recognisable"):
        build_names("A,B\n1,2\n")
    with pytest.raises(NamesError, match="only 4 companies"):
        build_names(equity_text())
    with pytest.raises(NamesError, match="missing TCS"):
        build_names(equity_text(extra=MIN_EQUITY_NAMES, drop=("TCS",)))


def test_the_sme_list_adds_names_but_never_overrides_the_equity_list():
    merged = build_names(BIG, SME + "RELIANCE,Not Reliance,SM,01-JAN-2020,10,1,X,10\n")
    assert merged["THEJO"] == "Thejo Engineering Limited" and merged["RELIANCE"] == "Reliance Industries Limited"


# ---------------------------------------------------------------- the store
@pytest.mark.parametrize("make", [MemoryStore, lambda: RedisStore(fakeredis.FakeStrictRedis(decode_responses=True))],
                         ids=["memory", "redis"])
def test_stored_names_round_trip_the_same_in_both_stores(make):
    store = make()
    assert store.get_names() == (None, {})
    store.set_names("2026-11-20", {"TCS": "Tata Consultancy Services Limited", "M&M": "Mahindra | Mahindra Limited"})
    assert store.get_names() == ("2026-11-20", {"TCS": "Tata Consultancy Services Limited",
                                                "M&M": "Mahindra / Mahindra Limited"})


def test_pack_names_is_compact():
    names = {f"SYM{i}": f"Some Company Number {i} Limited" for i in range(2600)}
    blob = pack_names(names)
    assert unpack_names(blob) == names and len(blob) < 60_000


# ---------------------------------------------------------------- the refresh
def test_refresh_stores_the_names_and_reports_counts():
    store = MemoryStore()
    counts = refresh_names(store, "2026-11-20", fetch=fake_fetch())
    assert counts == {"names": MIN_EQUITY_NAMES + 5, "previous": 0, "equity": MIN_EQUITY_NAMES + 4, "sme": 1}
    assert store.get_names()[0] == "2026-11-20" and store.get_names()[1]["INFY"] == "Infosys Limited"


def test_a_missing_sme_list_is_tolerated_but_a_missing_equity_list_is_not():
    store = MemoryStore()
    assert refresh_names(store, "2026-11-20", fetch=fake_fetch(sme=NamesError("HTTP 404")))["sme"] == 0
    with pytest.raises(NamesError, match="HTTP 403"):
        refresh_names(store, "2026-11-21", fetch=fake_fetch(equity=NamesError("HTTP 403")))
    assert store.get_names()[0] == "2026-11-20"                                   # the stored list was left alone


def test_a_much_smaller_list_never_replaces_a_good_one():
    store = MemoryStore()
    store.set_names("2026-11-19", {f"X{i}": "n" for i in range(5000)})
    with pytest.raises(NamesError, match="keeping the stored list"):                 # 1,505 new names against 5,000 stored
        refresh_names(store, "2026-11-20", fetch=fake_fetch())
    assert store.get_names()[0] == "2026-11-19" and len(store.get_names()[1]) == 5000


def test_the_cli_prints_counts_and_examples_and_exits_1_on_failure(capsys):
    store = MemoryStore()
    refresh_main(connect=lambda: (None, store, None), fetch=fake_fetch())
    out = capsys.readouterr().out
    assert "Stored 1505 names" in out and "RELIANCE: Reliance Industries Limited" in out and "TEMPSENS: (none)" in out
    with pytest.raises(SystemExit) as e:
        refresh_main(connect=lambda: (None, store, None), fetch=fake_fetch(equity=NamesError("HTTP 403 for EQUITY_L.csv")))
    assert e.value.code == 1 and "Names not updated: HTTP 403" in capsys.readouterr().out


# ---------------------------------------------------------------- answers use the stored name everywhere
def make(names=None, nse=None, store=None):
    return Assembler(store or build_store(), nse or FakeNse(), FakeNews(), RESOLVER, now=lambda: NOW, names=names)


FULL = {"ACME": "Acme Full Name Limited"}


def test_every_packet_kind_uses_the_stored_name_when_there_is_one():
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet")])
    assert make(FULL, nse).why_moved("acme").name == "Acme Full Name Limited"
    assert make(FULL, nse).events("acme").name == "Acme Full Name Limited"
    assert make(FULL, store=returns_store()).period_return("acme", "1w").name == "Acme Full Name Limited"
    refused = make(FULL, store=build_store(fillers=0)).period_return("acme", "1y")
    assert refused.status == "refused" and refused.name == "Acme Full Name Limited"


def test_without_a_stored_name_the_old_order_holds_filings_then_the_price_file_name():
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet")])
    assert make({}, nse).why_moved("acme").name == "Acme Industries Limited"          # the filing's company name
    assert make(None, FakeNse()).events("acme").name == "ACME INDUSTRIES LTD"          # nothing better: the price file
    assert make({"OTHER": "x"}, store=returns_store()).period_return("acme", "1w").name == "ACME INDUSTRIES LTD"


def test_a_stored_name_beats_the_filing_name_so_the_stock_is_never_called_two_things():
    nse = FakeNse(anns=[ann("2026-11-06T19:10:58", "Analyst Meet")])
    names = {make(FULL, nse).why_moved("acme").name, make(FULL, nse).events("acme").name,
             make(FULL, store=returns_store()).period_return("acme", "1w").name}
    assert names == {"Acme Full Name Limited"}


def test_choices_when_a_name_is_ambiguous_use_stored_names_too():
    p = make({"TATASTEEL": "Tata Steel Limited", "TATAPOWER": "The Tata Power Company Limited"}).why_moved("tata")
    assert p.status == "clarify"
    assert "Tata Steel Limited (TATASTEEL)" in p.choices and "The Tata Power Company Limited (TATAPOWER)" in p.choices
    assert "Tata Steel Limited (TATASTEEL)" in p.gaps[0].text
