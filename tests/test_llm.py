import json

import pytest

from briany import llm
from briany.llm import Chain, GeminiClient, GroqClient, LlmError, ToolCall

KEY = "gsk_SECRETKEY_123456"
TOOLS = [{"name": "why_moved", "description": "d", "parameters": {"type": "object", "properties": {
    "query": {"type": "string", "description": "q"}, "period": {"type": "string", "enum": ["1m", "3m"]}},
    "required": ["query"]}}]


class Transport:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def _next(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, headers, body, timeout):
        return self._next(url, headers, body)

    def get(self, url, headers, timeout):
        return self._next(url, headers, None)


def ok(obj):
    return 200, {}, json.dumps(obj)


def resp(status, text="", headers=None):
    return status, headers or {}, text


def groq_tool_reply(name="why_moved", args='{"query": "tata steel"}'):
    return ok({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}]}}]})


def groq(script, **kw):
    t, sleeps = Transport(script), []
    return GroqClient(KEY, post=t.post, sleep=sleeps.append, **kw), t, sleeps


def gemini(script, model="gemini-3.1-flash-lite", **kw):
    t, sleeps = Transport(script), []
    return GeminiClient(KEY, model=model, post=t.post, get=t.get, sleep=sleeps.append, **kw), t, sleeps


def fcall(name="why_moved", args=None):
    return ok({"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args or {"query": "tcs"}}}]}}]})


# ---------------------------------------------------------------- tool schemas
def test_schema_conversion_for_each_provider():
    g = llm.groq_tools(TOOLS)[0]["function"]["parameters"]
    assert g["type"] == "object" and g["properties"]["query"]["type"] == "string"
    gem = llm.gemini_tools(TOOLS)[0]["functionDeclarations"][0]["parameters"]
    assert gem["type"] == "OBJECT" and gem["properties"]["query"]["type"] == "STRING"
    assert gem["properties"]["period"]["enum"] == ["1m", "3m"]                       # enum values untouched
    assert TOOLS[0]["parameters"]["type"] == "object"                                # the source is not mutated


# ---------------------------------------------------------------- Groq
def test_groq_route_request_shape_and_parsing():
    c, t, _ = groq([groq_tool_reply()])
    assert c.route("sys", "why is tata steel up", TOOLS) == ToolCall("why_moved", {"query": "tata steel"})
    url, headers, body = t.calls[0]
    assert url.endswith("/chat/completions") and headers["Authorization"] == f"Bearer {KEY}"
    assert body["model"] == "openai/gpt-oss-120b" and body["tool_choice"] == "auto" and body["temperature"] == 0
    assert body["tools"][0]["type"] == "function" and body["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.parametrize("reply,kind", [
    (ok({"choices": [{"message": {"content": "Tata Steel rose today."}}]}), "no_tool_call"),
    (groq_tool_reply(args="{not json"), "invalid_route"),
    (groq_tool_reply(args="[1, 2]"), "invalid_route"),
    (ok({"unexpected": True}), "server"),
    (resp(200, "<html>"), "server")])
def test_groq_bad_replies_are_typed_errors(reply, kind):
    c, _, _ = groq([reply])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == kind


def test_groq_rate_limits_daily_limits_and_retries():
    c, _, sleeps = groq([resp(429, "rate limit reached", {"retry-after": "2"}), groq_tool_reply()])
    assert c.route("s", "u", TOOLS).name == "why_moved" and sleeps == [3.0]          # waits retry-after + 1, once
    c, _, sleeps = groq([resp(429, "slow down", {"retry-after": "60"})])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "rate_limit" and sleeps == []                              # too long to wait on serverless
    c, t, _ = groq([resp(429, "Rate limit reached ... tokens per day (TPD): Limit 100000")])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "daily_limit" and len(t.calls) == 1                        # not retried
    c, _, _ = groq([resp(429, "x", {"retry-after": "1"}), resp(429, "x", {"retry-after": "1"})])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "rate_limit"


def test_groq_server_errors_retry_once_and_auth_errors_do_not():
    c, t, sleeps = groq([resp(503), groq_tool_reply()])
    assert c.route("s", "u", TOOLS).name == "why_moved" and sleeps == [1] and len(t.calls) == 2
    c, _, _ = groq([resp(502), resp(502)])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "server"
    c, t, _ = groq([resp(401, "invalid api key")])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "auth" and len(t.calls) == 1 and "HTTP 401" in str(e.value)
    c, t, _ = groq([resp(403, "error code: 1010   Access  denied")])                 # a block page, not a bad key
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "forbidden" and "HTTP 403: error code: 1010 Access denied" in str(e.value) and len(t.calls) == 1


def test_errors_never_contain_the_key():
    c, _, _ = groq([resp(400, f"bad request for key {KEY} in body")])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "bad_request" and KEY not in str(e.value) and "***" in str(e.value)
    assert llm.redact(f"x {KEY} y", KEY) == "x *** y" and llm.redact("short", "abc") == "short"


def test_groq_write_strips_reasoning_and_rejects_empty():
    c, _, _ = groq([ok({"choices": [{"message": {"content": "<think>hmm</think>\nHello there."}}]})])
    assert c.write("s", "u") == "Hello there."
    c, _, _ = groq([ok({"choices": [{"message": {"content": "<think>only thinking</think>"}}]})])
    with pytest.raises(LlmError) as e:
        c.write("s", "u")
    assert e.value.kind == "empty"


def test_network_failures_are_typed():
    c, _, _ = groq([LlmError("network", "network error (TimeoutError)")])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "network"


# ---------------------------------------------------------------- Gemini
MODELS = ok({"models": [
    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]}]})


def test_pick_gemini_model_prefers_the_configured_order():
    assert llm.pick_gemini_model(["models/gemini-2.5-flash", "models/gemini-flash-latest"]) == "gemini-flash-latest"
    assert llm.pick_gemini_model(["models/gemini-3.1-flash-lite", "models/gemini-flash-latest"]) == "gemini-3.1-flash-lite"
    assert llm.pick_gemini_model(["models/gemini-9-flash-x", "models/gemini-9-pro"]) == "gemini-9-flash-x"
    assert llm.pick_gemini_model(["models/gemini-9-pro"]) is None


def test_gemini_discovers_a_model_once_then_routes():
    c, t, _ = gemini([MODELS, fcall(), fcall()], model="")
    assert c.route("sys", "why is tcs up", TOOLS) == ToolCall("why_moved", {"query": "tcs"})
    assert c.model == "gemini-flash-latest" and "models?pageSize=200" in t.calls[0][0]
    c.route("sys", "again", TOOLS)
    assert len(t.calls) == 3                                                          # no second discovery
    _, headers, body = t.calls[1]
    assert headers["x-goog-api-key"] == KEY and body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["tools"][0]["functionDeclarations"][0]["parameters"]["type"] == "OBJECT"
    assert t.calls[1][0].endswith("/models/gemini-flash-latest:generateContent")


def test_gemini_discovery_failures():
    c, _, _ = gemini([resp(403)], model="")
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "auth"
    c, _, _ = gemini([resp(403, "blocked")])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "forbidden"
    c, _, _ = gemini([ok({"models": [{"name": "models/x", "supportedGenerationMethods": ["embedContent"]}]})], model="")
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "bad_request"


@pytest.mark.parametrize("reply,kind", [
    (ok({"candidates": [{"content": {"parts": [{"text": "It rose."}]}}]}), "no_tool_call"),
    (ok({"candidates": []}), "empty"),
    (ok({"candidates": [{"content": {"parts": [{"functionCall": {"name": "x", "args": "oops"}}]}}]}), "invalid_route")])
def test_gemini_bad_replies(reply, kind):
    c, _, _ = gemini([reply])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == kind


def test_gemini_quota_handling():
    body_day = '{"error":{"details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}]}]}}'
    c, t, _ = gemini([resp(429, body_day)])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "daily_limit" and len(t.calls) == 1
    c, _, sleeps = gemini([resp(429, '{"retryDelay": "4s"}'), fcall()])
    assert c.route("s", "u", TOOLS).name == "why_moved" and sleeps == [5.0]
    c, _, sleeps = gemini([resp(429, '{"retryDelay": "40s"}')])
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "rate_limit" and sleeps == []
    c, _, sleeps = gemini([resp(503), fcall()])
    assert c.route("s", "u", TOOLS).name == "why_moved" and sleeps == [1]


def test_gemini_write():
    c, t, _ = gemini([ok({"candidates": [{"content": {"parts": [{"text": "Line one. "}, {"text": "Line two."}]}}]})])
    assert c.write("s", "u", max_tokens=300) == "Line one. Line two."
    assert t.calls[0][2]["generationConfig"]["maxOutputTokens"] == 300


# ---------------------------------------------------------------- chain
class Fake:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, 0

    def route(self, system, user, tools):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def write(self, system, user, max_tokens=500):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_chain_falls_through_failures_and_records_attempts():
    good = ToolCall("events", {"query": "tcs"})
    a, b = Fake("groq", error=LlmError("rate_limit")), Fake("gemini", good)
    call, who, attempts = Chain([a, b]).route("s", "u", TOOLS)
    assert call == good and who == "gemini" and attempts == [("groq", "rate_limit"), ("gemini", "ok")]
    first = Fake("groq", ToolCall("events", {}))
    second = Fake("gemini", good)

    def reject_empty(call):
        if not call.args.get("query"):
            raise ValueError("no query")
    call, who, attempts = Chain([first, second]).route("s", "u", TOOLS, validate=reject_empty)
    assert who == "gemini" and attempts == [("groq", "invalid_route"), ("gemini", "ok")]


def test_chain_all_failed_message_has_kinds_but_no_secrets():
    chain = Chain([Fake("groq", error=LlmError("daily_limit", f"HTTP 429 {KEY}")), Fake("gemini", error=LlmError("server"))])
    with pytest.raises(LlmError) as e:
        chain.route("s", "u", TOOLS)
    assert e.value.kind == "all_failed" and "groq=daily_limit" in str(e.value) and "gemini=server" in str(e.value)
    with pytest.raises(LlmError):
        Chain([]).route("s", "u", TOOLS)


def test_chain_write_with_validation():
    a, b = Fake("groq", "bad text"), Fake("gemini", "good text")
    text, who, attempts = Chain([a, b]).write("s", "u", validate=lambda t: (_ for _ in ()).throw(ValueError("x")) if "bad" in t else None)
    assert text == "good text" and who == "gemini" and attempts[0] == ("groq", "invalid_text")


# ---------------------------------------------------------------- Gemini model failover
def test_gemini_falls_back_to_a_second_model_when_the_first_is_overloaded():
    models = ok({"models": [{"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                            {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]}]})
    c, t, sleeps = gemini([models, resp(503), resp(503), fcall()], model="")
    assert c.route("s", "u", TOOLS).name == "why_moved"
    urls = [call[0] for call in t.calls[1:]]
    assert [u.split("/models/")[1].split(":")[0] for u in urls] == ["gemini-3.1-flash-lite"] * 2 + ["gemini-flash-latest"]
    assert c.model == "gemini-3.1-flash-lite" and c.fallbacks == ["gemini-flash-latest"]   # next call starts at the primary again


def test_failover_only_for_transient_errors_and_never_for_an_explicit_model():
    models = ok({"models": [{"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                            {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]}]})
    c, t, _ = gemini([models, resp(401)], model="")
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "auth" and len(t.calls) == 2                                  # a bad key is not retried elsewhere
    c, t, _ = gemini([resp(503), resp(503)], model="gemini-3.1-flash-lite")
    with pytest.raises(LlmError) as e:
        c.route("s", "u", TOOLS)
    assert e.value.kind == "server" and c.fallbacks == []
    assert llm.ordered_gemini_models(["models/gemini-9-flash-x", "models/gemini-flash-latest", "models/x-pro"]) == [
        "gemini-flash-latest", "gemini-9-flash-x"]


# ---------------------------------------------------------------- transport
def test_urllib_transport_sends_an_explicit_user_agent(monkeypatch):
    import builtins
    import urllib.request
    seen = {}

    class R:
        status = 200
        headers = {"X": "1"}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return R()
    real_import = builtins.__import__

    def no_requests(name, *a, **k):
        if name == "requests":
            raise ImportError
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_requests)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, _h, text = llm.default_post("https://example.invalid", {"Authorization": "Bearer x"}, {"a": 1}, 5)
    assert status == 200 and seen["ua"] == llm.USER_AGENT and "Python-urllib" not in seen["ua"]


def test_requests_transport_is_preferred_when_installed(monkeypatch):
    import sys
    import types
    calls = {}

    class Boom(Exception):
        pass
    fake = types.ModuleType("requests")
    fake.exceptions = types.SimpleNamespace(RequestException=Boom)

    def request(method, url, headers=None, json=None, timeout=None):
        calls.update(method=method, url=url, headers=headers, json=json, timeout=timeout)
        return types.SimpleNamespace(status_code=429, headers={"retry-after": "3"}, text="slow down")
    fake.request = request
    monkeypatch.setitem(sys.modules, "requests", fake)
    status, headers, text = llm.default_post("https://example.invalid/x", {"Authorization": "Bearer k"}, {"q": 1}, 7)
    assert (status, headers, text) == (429, {"retry-after": "3"}, "slow down")
    assert calls["method"] == "POST" and calls["json"] == {"q": 1} and calls["timeout"] == 7
    assert calls["headers"]["User-Agent"] == llm.USER_AGENT and calls["headers"]["Authorization"] == "Bearer k"

    def failing(*a, **k):
        raise Boom("x")
    fake.request = failing
    with pytest.raises(LlmError) as e:
        llm.default_get("https://example.invalid/x", {}, 5)
    assert e.value.kind == "network"
