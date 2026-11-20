# SPDX-License-Identifier: Apache-2.0
"""Full company names from NSE's equity lists, stored once a day so every answer calls a stock by the same name.

Checked on 20 Nov 2026: EQUITY_L.csv names the EQ, BE and BZ series (about 2,600 companies). The SME list was almost
empty, and the ETF list has no proper-name column, so SME stocks and ETFs keep the short name from the price file.
Names are cosmetic: a failure here never stops prices or answers, and an old stored list is never replaced by a worse one.
"""
from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request

ARCHIVE = "https://nsearchives.nseindia.com/content/equities/"
EQUITY_URL = ARCHIVE + "EQUITY_L.csv"
SME_URL = ARCHIVE + "SME_EQUITY_L.csv"
UA = {"User-Agent": "Mozilla/5.0"}
MIN_EQUITY_NAMES = 1500               # the real list has about 2,600; far fewer means a broken file
CANARIES = ("RELIANCE", "TCS", "INFY")
KEEP_AT_LEAST = 0.8                   # a new list smaller than this share of the stored one is rejected


class NamesError(RuntimeError):
    """The name list could not be fetched or looked wrong. The stored list is left as it was."""


def _norm(column: str) -> str:
    return "".join(ch for ch in column.upper() if ch.isalnum())


def read_names(text: str):
    """(columns, symbol_column, name_column, {symbol: name}) from an NSE list. Columns are found by header; a header
    that matches nothing gives None for that column and no names."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = [c.strip() for c in (reader.fieldnames or [])]
    reader.fieldnames = columns
    symbol_col = next((c for c in columns if _norm(c) == "SYMBOL"), None) or next(
        (c for c in columns if "SYMBOL" in _norm(c)), None)
    name_col = (next((c for c in columns if _norm(c) in ("NAMEOFCOMPANY", "COMPANYNAME", "SECURITYNAME")), None)
                or next((c for c in columns if "NAME" in _norm(c) and c != symbol_col), None))
    names = {}
    if symbol_col and name_col:
        for row in reader:
            sym, name = (row.get(symbol_col) or "").strip(), " ".join((row.get(name_col) or "").split())
            if sym and name:
                names[sym] = name
    return columns, symbol_col, name_col, names


def fetch_text(url: str, timeout: int = 30) -> str:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise NamesError(f"HTTP {e.code} for {url.rsplit('/', 1)[1]}") from None
    except Exception as e:  # noqa: BLE001
        raise NamesError(f"{type(e).__name__} for {url.rsplit('/', 1)[1]}") from None


def build_names(equity_text: str, sme_text: str = "") -> dict:
    """Merge the lists (equity wins). Raises NamesError if the main list looks wrong."""
    _cols, sym_col, name_col, names = read_names(equity_text)
    if not (sym_col and name_col):
        raise NamesError("EQUITY_L.csv has no recognisable symbol and name columns")
    if len(names) < MIN_EQUITY_NAMES:
        raise NamesError(f"EQUITY_L.csv names only {len(names)} companies (expected at least {MIN_EQUITY_NAMES})")
    missing = [c for c in CANARIES if c not in names]
    if missing:
        raise NamesError("EQUITY_L.csv is missing " + ", ".join(missing))
    merged = dict(read_names(sme_text)[3]) if sme_text else {}
    merged.update(names)
    return merged


def refresh_names(store, today: str, fetch=fetch_text) -> dict:
    """Fetch, check and store the names. Returns counts. Raises NamesError and keeps the old list on any problem."""
    equity = fetch(EQUITY_URL)
    try:
        sme = fetch(SME_URL)
    except NamesError:
        sme = ""                                                  # optional: the SME list was nearly empty anyway
    names = build_names(equity, sme)
    _day, old = store.get_names()
    if old and len(names) < len(old) * KEEP_AT_LEAST:
        raise NamesError(f"new list has {len(names)} names but {len(old)} are stored; keeping the stored list")
    store.set_names(today, names)
    return {"names": len(names), "previous": len(old), "equity": len(read_names(equity)[3]),
            "sme": len(read_names(sme)[3]) if sme else 0}
