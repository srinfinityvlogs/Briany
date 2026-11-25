# SPDX-License-Identifier: Apache-2.0
from types import SimpleNamespace

import fakeredis
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient                      # noqa: E402

import app as web                                               # noqa: E402
from briany.router import Context                               # noqa: E402
from briany.telegram_bot import MAX_QUESTION_CHARS, RATE_MINUTE, State   # noqa: E402
from briany.webchat import BSE_NOTE, FNO_REPLY, WebChat, clean_tab       # noqa: E402

SECRET = "chat-secret-123"


class FakeBot:
    def __init__(self):
        self.asked = []

    def ask(self, question, context=None):
        self.asked.append((question, context))
        return SimpleNamespace(text="TCS closed at 2,376.20.", kind="return", writer="code", problems=(),
                               route=SimpleNamespace(tool="period_return"), context=Context("TCS", "Tata Consultancy Services"))


def make(per_minute=3, per_day=40):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    bot = FakeBot()
    return WebChat(bot, State(r, per_minute=per_minute, per_day=per_day)), bot, r


def test_a_normal_question_reaches_the_bot_and_the_answer_comes_back_as_text():
    chat, bot, _ = make()
    out = chat.ask("TCS 1 year return", "fno")
    assert out == {"ok": True, "text": "TCS closed at 2,376.20.", "kind": "return", "limited": "", "note": ""}
    assert [q for q, _ in bot.asked] == ["TCS 1 year return"]


def test_the_follow_up_context_is_kept_for_the_web_user():
    chat, bot, _ = make()
    chat.ask("TCS 1 year return")
    chat.ask("and 3 months?")
    assert bot.asked[1][1].last_symbol == "TCS"


@pytest.mark.parametrize("q", ["open interest in Reliance", "what is the OI buildup?", "show me the option chain", "PCR today",
                               "which strike has max pain", "Nifty futures", "Reliance resistance", "short build-up stocks"])
def test_futures_and_options_questions_never_reach_the_bot(q):
    chat, bot, _ = make()
    out = chat.ask(q, "nse")
    assert out["ok"] and out["kind"] == "fixed" and out["text"] == FNO_REPLY["nse"] and bot.asked == []


def test_the_reply_follows_the_tab():
    chat, _, _ = make()
    assert "price-only" in chat.ask("open interest", "bse")["text"]
    assert "price-only" in chat.ask("open interest", "nse")["text"]
    assert "can't read them yet" in chat.ask("open interest", "nifty50")["text"]
    assert "can't read them yet" in chat.ask("open interest", "fno")["text"]
    assert "I don't have open interest" in chat.ask("open interest", None)["text"]
    assert "I don't have open interest" in chat.ask("open interest", "weird")["text"]


def test_words_that_only_look_like_futures_words_are_answered_normally():
    chat, bot, _ = make()
    chat.ask("TCS announcements on Friday")
    chat.ask("why did Infosys fall, any support from the board?")
    assert len(bot.asked) == 2


def test_futures_questions_do_not_use_the_daily_allowance():
    chat, _, r = make(per_minute=1)
    for _ in range(5):
        assert chat.ask("open interest", "fno")["limited"] == ""
    assert chat.ask("TCS 1 year return")["ok"]


def test_the_bse_tab_adds_its_note_only_to_real_answers():
    chat, _, _ = make()
    assert chat.ask("TCS 1 year return", "bse")["note"] == BSE_NOTE
    assert chat.ask("TCS 1 year return", "nse")["note"] == ""
    assert chat.ask("", "bse")["note"] == ""


def test_empty_and_overlong_questions_are_refused_without_the_bot():
    chat, bot, _ = make()
    assert not chat.ask("   ")["ok"] and not chat.ask(None)["ok"] and not chat.ask(42)["ok"]
    assert not chat.ask("x" * (MAX_QUESTION_CHARS + 1))["ok"]
    assert chat.ask("x" * MAX_QUESTION_CHARS)["ok"]
    assert len(bot.asked) == 1


def test_the_minute_limit_stops_the_bot_and_says_so():
    chat, bot, _ = make(per_minute=2)
    assert chat.ask("a")["ok"] and chat.ask("b")["ok"]
    out = chat.ask("c")
    assert not out["ok"] and out["limited"] == "minute" and out["text"] == RATE_MINUTE and len(bot.asked) == 2


def test_the_web_limits_are_separate_from_a_telegram_user():
    chat, _, r = make(per_minute=1)
    State(r, per_minute=1).question_limit(111)
    assert chat.ask("a")["ok"]


def test_a_bot_that_raises_gives_a_fixed_error_and_no_message_text(caplog):
    chat, bot, _ = make()
    bot.ask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom with secret words"))
    out = chat.ask("TCS 1 year return")
    assert not out["ok"] and out["kind"] == "error" and "secret words" not in caplog.text and "TCS" not in caplog.text


def test_clean_tab():
    assert [clean_tab(t) for t in ("nifty50", "fno", "nse", "bse")] == ["nifty50", "fno", "nse", "bse"]
    assert clean_tab("x") is None and clean_tab(None) is None and clean_tab(3) is None


# ---------------------------------------------------------------- the HTTP route
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_CHAT_SECRET", SECRET)
    monkeypatch.setattr(web, "_web", None)
    return TestClient(web.app)


def ask(client, body=None, secret=SECRET, **kw):
    headers = {} if secret is None else {"X-Chat-Secret": secret}
    return client.post("/ask", json={"question": "hi", "tab": "fno"} if body is None else body, headers=headers, **kw)


def test_ask_is_refused_before_anything_is_built_without_the_right_secret(client, monkeypatch):
    monkeypatch.setattr(web, "get_web", lambda: (_ for _ in ()).throw(AssertionError("must not be built")))
    assert ask(client, secret=None).status_code == 403
    assert ask(client, secret="wrong").status_code == 403
    assert client.post("/ask", json={}, headers={"X-Chat-Secret": "wrong-é".encode("latin-1")}).status_code == 403


def test_a_missing_server_secret_turns_the_web_chat_off(client, monkeypatch):
    monkeypatch.setenv("WEB_CHAT_SECRET", "")
    assert ask(client, secret="").status_code == 403 and ask(client, secret=None).status_code == 403


def test_ask_passes_question_and_tab_and_returns_the_result(client, monkeypatch):
    seen = []

    class Fake:
        def ask(self, q, tab):
            seen.append((q, tab))
            return {"ok": True, "text": "answer", "kind": "return", "limited": "", "note": ""}
    monkeypatch.setattr(web, "get_web", lambda: Fake())
    r = ask(client, {"question": "TCS 1 year return", "tab": "bse"})
    assert r.status_code == 200 and r.json()["text"] == "answer" and seen == [("TCS 1 year return", "bse")]


def test_a_limited_result_is_a_429(client, monkeypatch):
    monkeypatch.setattr(web, "get_web", lambda: SimpleNamespace(ask=lambda q, t: {"ok": False, "text": "slow down", "kind": "fixed",
                                                                                  "limited": "minute", "note": ""}))
    assert ask(client).status_code == 429


def test_bad_bodies_are_refused(client, monkeypatch):
    monkeypatch.setattr(web, "get_web", lambda: SimpleNamespace(ask=lambda q, t: {"ok": True, "text": "", "kind": "", "limited": "", "note": ""}))
    assert client.post("/ask", content=b"not json", headers={"X-Chat-Secret": SECRET}).status_code == 400
    assert ask(client, body=["a list"]).status_code == 400
    assert ask(client, body={"question": "x" * 5000}).status_code == 413


def test_a_chat_that_cannot_be_built_gives_503_and_no_secret(client, monkeypatch, caplog):
    from briany.config import ConfigError
    monkeypatch.setattr(web, "get_web", lambda: (_ for _ in ()).throw(ConfigError("Missing required environment variables: GROQ_API_KEY")))
    r = ask(client)
    assert r.status_code == 503 and "GROQ_API_KEY" in caplog.text and "GROQ_API_KEY" not in r.text and SECRET not in caplog.text


def test_ask_only_accepts_post(client):
    assert client.get("/ask").status_code == 405


# ---------------------------------------------------------------- the real wiring, with fake Redis
def test_build_webchat_wires_the_real_bot_and_a_futures_question_is_answered_without_a_model():
    from briany.runtime import build_webchat
    from briany.store import RedisStore
    from briany.symbols import Instrument
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    RedisStore(r).set_instruments("2026-11-09", [Instrument("ACME", "ACME INDUSTRIES LTD")])
    env = {"TELEGRAM_BOT_TOKEN": "123456:TOKEN", "TELEGRAM_WEBHOOK_SECRET": "w", "GROQ_API_KEY": "gsk_test_123456",
           "KV_REDIS_URL": "rediss://default:pw@host:6379", "RATE_LIMIT_PER_MINUTE": "7"}
    chat = build_webchat(env, client=r)
    assert chat.state.per_minute == 7 and chat.uid == 0
    assert chat.ask("open interest in ACME", "fno")["text"] == FNO_REPLY["fno"]


def test_build_webchat_says_what_is_missing_without_values():
    from briany.config import ConfigError
    from briany.runtime import build_webchat
    with pytest.raises(ConfigError, match="GROQ_API_KEY") as e:
        build_webchat({"TELEGRAM_BOT_TOKEN": "123456:TOKEN"}, client=fakeredis.FakeStrictRedis(decode_responses=True))
    assert "123456" not in str(e.value)


def test_an_error_answer_on_the_bse_tab_carries_no_bse_note():
    chat, bot, _ = make()
    orig = bot.ask
    bot.ask = lambda q, c=None: SimpleNamespace(**{**orig(q, c).__dict__, "kind": "error", "text": "Something went wrong."})
    out = chat.ask("TCS 1 year return", "bse")
    assert not out["ok"] and out["note"] == ""
