# SPDX-License-Identifier: Apache-2.0
import fakeredis
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient            # noqa: E402

import app as web                                     # noqa: E402
from briany.config import ConfigError                 # noqa: E402
from briany.llm import ToolCall                       # noqa: E402
from briany.runtime import build_handler              # noqa: E402
from briany.store import RedisStore                   # noqa: E402
from briany.symbols import Instrument                 # noqa: E402

SECRET = "webhook-secret-123"
ENV = {"TELEGRAM_BOT_TOKEN": "123456:TOKEN", "TELEGRAM_WEBHOOK_SECRET": SECRET, "GROQ_API_KEY": "gsk_test_123456",
       "KV_REDIS_URL": "rediss://default:pw@host:6379", "ALLOWED_TELEGRAM_IDS": "111"}
UPDATE = {"update_id": 1, "message": {"from": {"id": 111, "is_bot": False}, "chat": {"id": 111, "type": "private"},
                                       "text": "hello"}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(web, "_handler", None)
    return TestClient(web.app)


class Recorder:
    def __init__(self):
        self.updates = []

    def handle(self, update):
        self.updates.append(update)
        return "answered"


def post(client, body=UPDATE, secret=SECRET, **kw):
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return client.post("/telegram", json=body, headers=headers, **kw)


def test_requests_without_the_right_secret_are_refused_before_anything_is_built(client, monkeypatch):
    def boom():
        raise AssertionError("handler must not be built for an unauthenticated request")
    monkeypatch.setattr(web, "get_handler", boom)
    assert post(client, secret=None).status_code == 403
    assert post(client, secret="wrong").status_code == 403
    odd = client.post("/telegram", json=UPDATE, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-é".encode("latin-1")})
    assert odd.status_code == 403                                                    # non-ASCII must not crash the comparison


def test_a_missing_server_secret_refuses_everything(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    assert post(client, secret="").status_code == 403 and post(client, secret=None).status_code == 403


def test_a_valid_update_is_handed_to_the_handler_and_answered_with_200(client, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(web, "get_handler", lambda: rec)
    r = post(client)
    assert r.status_code == 200 and r.json() == {"ok": True} and rec.updates == [UPDATE]


def test_a_body_that_is_not_json_gets_200_and_no_retry(client, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(web, "get_handler", lambda: rec)
    r = client.post("/telegram", content=b"not json", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert r.status_code == 200 and rec.updates == []


def test_a_handler_that_cannot_be_built_gives_503_and_logs_no_secret(client, monkeypatch, caplog):
    def broken():
        raise ConfigError("Missing required environment variables: GROQ_API_KEY")
    monkeypatch.setattr(web, "get_handler", broken)
    r = post(client)
    assert r.status_code == 503 and "GROQ_API_KEY" in caplog.text and "GROQ_API_KEY" not in r.text


def test_nothing_else_is_exposed(client):
    assert client.get("/").json() == {"ok": True}
    for path in ("/docs", "/openapi.json", "/redoc", "/spike", "/spike2", "/storage"):
        assert client.get(path).status_code == 404
    assert client.get("/telegram").status_code == 405


# ---------------------------------------------------------------- the real wiring, with fake Redis, Telegram and Groq
class FakeGroq:
    name = "groq"

    def __init__(self, *a, **k):
        pass

    def route(self, system, user, tools):
        return ToolCall("smalltalk", {})

    def write(self, *a, **k):
        raise AssertionError("no writing for smalltalk")


def fake_redis_with_instruments():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    RedisStore(r).set_instruments("2026-11-09", [Instrument("ACME", "ACME INDUSTRIES LTD")])
    return r


def test_build_handler_wires_everything_and_a_webhook_call_reaches_telegram(client, monkeypatch):
    monkeypatch.setattr("briany.llm.GroqClient", FakeGroq)                            # no network
    sent = []
    handler = build_handler(ENV, client=fake_redis_with_instruments(),
                            api_post=lambda url, payload, timeout: (sent.append(payload) or (200, "")))
    monkeypatch.setattr(web, "get_handler", lambda: handler)
    assert post(client).status_code == 200
    assert [p["text"] for p in sent if "text" in p][0].startswith("I answer questions about one NSE-listed stock")


def test_build_handler_reads_the_allowlist_and_limits_from_the_environment():
    env = dict(ENV, ALLOWED_TELEGRAM_IDS="111, 222", RATE_LIMIT_PER_MINUTE="2", RATE_LIMIT_PER_DAY="10",
               CONTEXT_TTL_SECONDS="600")
    h = build_handler(env, client=fake_redis_with_instruments())
    assert h.allowed == {111, 222}
    assert (h.state.per_minute, h.state.per_day, h.state.context_ttl) == (2, 10, 600)


def test_build_handler_says_what_is_missing_without_values():
    with pytest.raises(ConfigError) as e:
        build_handler({"TELEGRAM_BOT_TOKEN": "123456:TOKEN"}, client=fakeredis.FakeStrictRedis(decode_responses=True))
    assert "GROQ_API_KEY" in str(e.value) and "123456:TOKEN" not in str(e.value)
    with pytest.raises(ConfigError) as e:
        build_handler(ENV, client=fakeredis.FakeStrictRedis(decode_responses=True))   # Redis has no instrument list
    assert "instrument list" in str(e.value)


def test_a_malformed_redis_url_is_reported_without_echoing_it():
    with pytest.raises(ConfigError) as e:
        build_handler(dict(ENV, KV_REDIS_URL="rediss://default:hunter2password"))
    assert "hunter2password" not in str(e.value) and "@HOST" in str(e.value)


def test_gemini_joins_the_chain_only_when_its_key_is_set(monkeypatch):
    monkeypatch.setattr("briany.llm.GroqClient", FakeGroq)
    without = build_handler(ENV, client=fake_redis_with_instruments())
    with_key = build_handler(dict(ENV, GEMINI_API_KEY="gem_key_123456"), client=fake_redis_with_instruments())
    assert [p.name for p in without.bot.chain.providers] == ["groq"]
    assert [p.name for p in with_key.bot.chain.providers] == ["groq", "gemini"]
