import pytest

from briany.symbols import Instrument, Resolver, format_choices, parse_bhavcopy

# Names below are copied from NSE's 6 Nov 2026 file (abbreviations included),
# except AXISGOLD, MAPMYINDIA-style noise and turnover figures, which are illustrative.
FIX = [
    Instrument("RELIANCE", "RELIANCE INDUSTRIES LTD", turnover=2.5e10),
    Instrument("RPOWER", "RELIANCE POWER LTD", turnover=9e8),
    Instrument("RELINFRA", "RELIANCE INFRASTRUCTURE LTD", turnover=4e8),
    Instrument("TCS", "TATA CONSULTANCY SERV LT", turnover=8e9),
    Instrument("TATASTEEL", "TATA STEEL LIMITED", turnover=6e9),
    Instrument("TATAPOWER", "TATA POWER CO LTD", turnover=3e9),
    Instrument("TATAELXSI", "TATA ELXSI LIMITED", turnover=1e9),
    Instrument("INFY", "INFOSYS LIMITED", turnover=7e9),
    Instrument("NAUKRI", "INFO EDGE (I) LTD", turnover=2e9),
    Instrument("HDFCBANK", "HDFC BANK LTD", turnover=9e9),
    Instrument("HDFCAMC", "HDFC AMC LIMITED", turnover=1e9),
    Instrument("HDFCLIFE", "HDFC LIFE INS CO LTD", turnover=2e9),
    Instrument("TEMPSENS", "TEMPSENS INST (INDIA) LTD", turnover=1e7),
    Instrument("KRN", "KRN HEAT EXCHANGE N REF L", turnover=2e8),
    Instrument("GOLDBEES", "NIP IND ETF GOLD BEES", turnover=5.4e9),
    Instrument("AXISGOLD", "AXIS GOLD ETF", turnover=1e8),
]
R = Resolver(FIX)


def sym(q):
    r = R.resolve(q)
    return r.status, (r.best.symbol if r.best else None)


@pytest.mark.parametrize("q,expected", [
    ("TCS", ("exact", "TCS")),
    ("tcs", ("exact", "TCS")),
    ("reliance", ("exact", "RELIANCE")),
    ("goldbees", ("exact", "GOLDBEES")),
    ("gold bees", ("exact", "GOLDBEES")),
    ("krn", ("exact", "KRN")),
    ("tempsens", ("exact", "TEMPSENS")),
    ("nifty", ("exact", "NIFTY 50")),
    ("Bank Nifty", ("exact", "NIFTY BANK")),
])
def test_exact_and_aliases(q, expected):
    assert sym(q) == expected


def test_name_matches():
    assert sym("tata consultancy")[1] == "TCS"
    assert sym("infosys")[1] == "INFY"
    assert sym("reliance power")[1] == "RPOWER"
    assert sym("tempsens instruments")[1] == "TEMPSENS"


def test_typo_resolves_but_is_reported_as_non_exact():
    status, symbol = sym("relience")
    assert symbol == "RELIANCE" and status in {"confident", "ambiguous"}


def test_brand_prefix_is_ambiguous_never_guessed():
    r = R.resolve("tata")
    assert r.status == "ambiguous"
    assert {m.instrument.symbol for m in r.matches} >= {"TATASTEEL", "TATAPOWER"}
    assert "Did you mean" in format_choices(r)


def test_generic_gold_etf_asks():
    assert R.resolve("gold etf").status == "ambiguous"


def test_full_names_match_abbreviated_nse_names():
    assert sym("tata consultancy services")[1] == "TCS"
    assert sym("krn heat exchanger")[1] == "KRN"
    assert sym("nippon india etf gold")[1] == "GOLDBEES"


def test_typo_lists_only_close_candidates():
    r = R.resolve("infosis")
    assert r.status == "ambiguous"
    assert [m.instrument.symbol for m in r.matches] == ["INFY"]
    assert "Did you mean INFOSYS LIMITED (INFY)?" in format_choices(r)


def test_hdfc_asks():
    r = R.resolve("hdfc")
    assert r.status == "ambiguous"
    assert {m.instrument.symbol for m in r.matches} == {"HDFCBANK", "HDFCAMC", "HDFCLIFE"}


def test_unknown_and_short_queries():
    assert R.resolve("zzzzqqq").status == "none"
    assert R.resolve("").status == "none"
    assert R.resolve("a").status == "none"
    assert "couldn't find" in format_choices(R.resolve("zzzzqqq"))


def test_duplicate_symbol_keeps_most_traded_series():
    r = Resolver([Instrument("ABC", "ABC LTD", series="BE", turnover=1),
                  Instrument("ABC", "ABC LTD", series="EQ", turnover=5)])
    assert r.resolve("abc").best.series == "EQ"


def test_parse_bhavcopy_filters_series_and_reports_stats():
    csv_text = (
        "TradDt,TckrSymb,SctySrs,FinInstrmTp,FinInstrmNm,ISIN,TtlTrfVal\n"
        "2026-11-04,RELIANCE,EQ,STK,RELIANCE INDUSTRIES LTD,INE002A01018,25450546878.20\n"
        "2026-11-04,SOMEBOND,N1,STK,SOME BOND,INE000000000,100\n"
        "2026-11-04,GOLDBEES,EQ,STK,NIPPON INDIA ETF GOLD BEES,INF204KB17I5,1000\n")
    inst, stats = parse_bhavcopy(csv_text)
    assert {i.symbol for i in inst} == {"RELIANCE", "GOLDBEES"}
    assert stats["rows"] == 3 and stats["series"]["N1"] == 1


def test_rights_entitlements_match_only_when_named_exactly():
    r = Resolver([Instrument("PARTY", "PARTY INDUSTRIES LTD", turnover=1e8),
                  Instrument("PARTY-RE", "PARTY INDUSTRIES LTD RIGHTS ENTITLEMENT", turnover=1e5),
                  Instrument("OLAELEC-RE1", "OLA ELECTRIC RIGHTS ENT", turnover=1e5)])
    assert sym_of(r, "party") == ("exact", "PARTY")
    assert sym_of(r, "party industries")[1] == "PARTY"
    assert sym_of(r, "party-re") == ("exact", "PARTY-RE")
    assert r.resolve("ola electric").status == "none"          # only the entitlement exists: not offered


def sym_of(resolver, query):
    res = resolver.resolve(query)
    return res.status, (res.best.symbol if res.best else None)
