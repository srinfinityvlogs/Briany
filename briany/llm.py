"""LLM providers (Groq, Gemini) behind one interface, with a fallback chain.

Phase 1 verified: Groq `chat/completions` with OpenAI-style tools (openai/gpt-oss-120b chose the right tool
15/15 times); Gemini `generateContent` with function declarations, model discovery via `models`, and 429s whose
body carries a retryDelay or a PerDay quota marker. Errors never contain a key. Stdlib only.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

GROQ_API = "https://api.groq.com/openai/v1"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GROQ_MODELS = ("openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b")
GEMINI_PREFERRED = ("gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest",
                    "gemini-3.5-flash", "gemini-2.5-flash")


class LlmError(RuntimeError):
    """kind: network | rate_limit | daily_limit | server | auth | bad_request | empty | no_tool_call
    | invalid_route | all_failed"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


def redact(text: str, *secrets) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text


# ------------------------------------------------------------------ transport (injectable for tests)
def _http(url, headers, body, timeout, method):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, dict(e.headers or {}), text
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmError("network", f"network error ({type(e).__name__})") from None


def default_post(url, headers, body, timeout):
    return _http(url, {**headers, "Content-Type": "application/json"}, body, timeout, "POST")


def default_get(url, headers, timeout):
    return _http(url, headers, None, timeout, "GET")


# ------------------------------------------------------------------ tool schema conversion
def _map_types(obj, fn):
    if isinstance(obj, dict):
        return {k: (fn(v) if k == "type" and isinstance(v, str) else _map_types(v, fn)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_types(x, fn) for x in obj]
    return obj


def groq_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": _map_types(t["parameters"], str.lower)}} for t in tools]


def gemini_tools(tools):
    return [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                       "parameters": _map_types(t["parameters"], str.upper)} for t in tools]}]


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _wait_seconds(headers, text, cap):
    """Seconds to wait before one retry, or None if waiting is not worthwhile."""
    candidates = []
    ra = {k.lower(): v for k, v in (headers or {}).items()}.get("retry-after")
    if ra:
        try:
            candidates.append(float(ra) + 1)
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text or "")
    if m:
        candidates.append(float(m.group(1)) + 1)
    if not candidates:
        return None
    wait = max(candidates)
    return wait if wait <= cap else None


def _json(text):
    try:
        return json.loads(text)
    except ValueError:
        raise LlmError("server", "response was not valid JSON") from None


# ------------------------------------------------------------------ Groq
class GroqClient:
    name = "groq"

    def __init__(self, key, model=GROQ_MODELS[0], post=default_post, sleep=time.sleep, max_wait=8.0, timeout=20):
        self.key, self.model, self._post, self._sleep = key, model, post, sleep
        self.max_wait, self.timeout = max_wait, timeout

    def _request(self, body):
        headers = {"Authorization": f"Bearer {self.key}"}
        for attempt in range(2):
            status, rh, text = self._post(f"{GROQ_API}/chat/completions", headers, body, self.timeout)
            if status == 200:
                return _json(text)
            safe = redact(text, self.key)[:300]
            if status == 429:
                if re.search(r"per day|\bTPD\b|\bRPD\b", text, re.I):
                    raise LlmError("daily_limit", f"HTTP 429: {safe}")
                wait = _wait_seconds(rh, text, self.max_wait)
                if attempt == 0 and wait is not None:
                    self._sleep(wait)
                    continue
                raise LlmError("rate_limit", f"HTTP 429: {safe}")
            if status >= 500:
                if attempt == 0:
                    self._sleep(1)
                    continue
                raise LlmError("server", f"HTTP {status}")
            if status in (401, 403):
                raise LlmError("auth", f"HTTP {status}")
            raise LlmError("bad_request", f"HTTP {status}: {safe}")
        raise LlmError("server", "no response")

    def _message(self, data):
        try:
            return data["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError):
            raise LlmError("server", "unexpected response shape") from None

    def route(self, system, user, tools) -> ToolCall:
        body = {"model": self.model, "temperature": 0, "tool_choice": "auto", "tools": groq_tools(tools),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        calls = self._message(self._request(body)).get("tool_calls") or []
        if not calls:
            raise LlmError("no_tool_call", "the model answered in text instead of calling a tool")
        fn = calls[0].get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            raise LlmError("invalid_route", "tool arguments were not valid JSON") from None
        if not isinstance(args, dict) or not fn.get("name"):
            raise LlmError("invalid_route", "tool call was malformed")
        return ToolCall(fn["name"], args)

    def write(self, system, user, max_tokens=500) -> str:
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        text = strip_think(self._message(self._request(body)).get("content") or "")
        if not text:
            raise LlmError("empty", "the model returned no text")
        return text


# ------------------------------------------------------------------ Gemini
def pick_gemini_model(names):
    """First preferred model that exists; else any flash model; else None."""
    have = [n.split("/")[-1] for n in names]
    for want in GEMINI_PREFERRED:
        if want in have:
            return want
    flash = sorted(n for n in have if "flash" in n)
    return flash[0] if flash else None


class GeminiClient:
    name = "gemini"

    def __init__(self, key, model="", post=default_post, get=default_get, sleep=time.sleep, max_wait=8.0, timeout=25):
        self.key, self.model, self._post, self._get = key, model, post, get
        self._sleep, self.max_wait, self.timeout = sleep, max_wait, timeout

    def discover_model(self):
        status, _h, text = self._get(f"{GEMINI_API}/models?pageSize=200", {"x-goog-api-key": self.key}, self.timeout)
        if status != 200:
            raise LlmError("auth" if status in (401, 403) else "server", f"model list HTTP {status}")
        names = [m["name"] for m in _json(text).get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        model = pick_gemini_model(names)
        if not model:
            raise LlmError("bad_request", "no usable Gemini model found")
        return model

    def _request(self, body):
        if not self.model:
            self.model = self.discover_model()
        url = f"{GEMINI_API}/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.key}
        for attempt in range(2):
            status, rh, text = self._post(url, headers, body, self.timeout)
            if status == 200:
                return _json(text)
            safe = redact(text, self.key)[:300]
            if status == 429:
                if "PerDay" in text:
                    raise LlmError("daily_limit", "Gemini daily quota exhausted")
                wait = _wait_seconds(rh, text, self.max_wait)
                if attempt == 0 and wait is not None:
                    self._sleep(wait)
                    continue
                raise LlmError("rate_limit", f"HTTP 429: {safe}")
            if status >= 500:
                if attempt == 0:
                    self._sleep(1)
                    continue
                raise LlmError("server", f"HTTP {status}")
            if status in (401, 403):
                raise LlmError("auth", f"HTTP {status}")
            raise LlmError("bad_request", f"HTTP {status}: {safe}")
        raise LlmError("server", "no response")

    @staticmethod
    def _parts(data):
        try:
            return data["candidates"][0]["content"]["parts"] or []
        except (KeyError, IndexError, TypeError):
            raise LlmError("empty", "Gemini returned no content") from None

    def route(self, system, user, tools) -> ToolCall:
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "tools": gemini_tools(tools)}
        for part in self._parts(self._request(body)):
            fc = part.get("functionCall")
            if fc and fc.get("name"):
                args = fc.get("args") or {}
                if not isinstance(args, dict):
                    raise LlmError("invalid_route", "tool call was malformed")
                return ToolCall(fc["name"], dict(args))
        raise LlmError("no_tool_call", "the model answered in text instead of calling a tool")

    def write(self, system, user, max_tokens=500) -> str:
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}}
        text = "".join(p.get("text", "") for p in self._parts(self._request(body))).strip()
        if not text:
            raise LlmError("empty", "the model returned no text")
        return text


# ------------------------------------------------------------------ fallback chain
class Chain:
    """Try providers in order. Any failure, including a route the validator rejects, moves to the next."""

    def __init__(self, providers):
        self.providers = list(providers)

    def route(self, system, user, tools, validate=None):
        attempts = []
        for p in self.providers:
            try:
                call = p.route(system, user, tools)
                if validate:
                    validate(call)
                attempts.append((p.name, "ok"))
                return call, p.name, attempts
            except LlmError as e:
                attempts.append((p.name, e.kind))
            except ValueError:
                attempts.append((p.name, "invalid_route"))
        raise LlmError("all_failed", ", ".join(f"{n}={k}" for n, k in attempts) or "no providers configured")

    def write(self, system, user, max_tokens=500, validate=None):
        attempts = []
        for p in self.providers:
            try:
                text = p.write(system, user, max_tokens)
                if validate:
                    validate(text)
                attempts.append((p.name, "ok"))
                return text, p.name, attempts
            except LlmError as e:
                attempts.append((p.name, e.kind))
            except ValueError:
                attempts.append((p.name, "invalid_text"))
        raise LlmError("all_failed", ", ".join(f"{n}={k}" for n, k in attempts) or "no providers configured")
