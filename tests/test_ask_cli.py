# SPDX-License-Identifier: Apache-2.0
import briany.ingest
from briany.ask_cli import build_bot
from briany.store import MemoryStore
from briany.symbols import Instrument


def test_the_ask_bot_workflow_uses_the_stored_company_names(monkeypatch):
    store = MemoryStore()
    store.set_instruments("2026-11-18", [Instrument("TCS", "TATA CONSULTANCY SERV LT")])
    store.set_names("2026-11-20", {"TCS": "Tata Consultancy Services Limited"})
    monkeypatch.setattr(briany.ingest, "connect", lambda: (None, store, None))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_123456")
    assert build_bot().asm.names == {"TCS": "Tata Consultancy Services Limited"}
