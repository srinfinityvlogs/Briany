# SPDX-License-Identifier: Apache-2.0
"""Vercel entry point: the Telegram webhook. Everything else lives in the briany package."""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

log = logging.getLogger("briany.app")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

REBUILD_AFTER_SECONDS = 6 * 3600        # a long-lived instance reloads the instrument list now and then
_lock = threading.Lock()
_handler, _built_at = None, 0.0
_web, _web_built_at = None, 0.0
MAX_ASK_BYTES = 4096


def get_handler():
    """Build once per process (cold start), rebuild every few hours."""
    global _handler, _built_at
    with _lock:
        if _handler is None or time.monotonic() - _built_at > REBUILD_AFTER_SECONDS:
            from briany.runtime import build_handler
            _handler, _built_at = build_handler(), time.monotonic()
    return _handler


def get_web():
    """The web chat, built like the Telegram handler: once per process, rebuilt every few hours."""
    global _web, _web_built_at
    with _lock:
        if _web is None or time.monotonic() - _web_built_at > REBUILD_AFTER_SECONDS:
            from briany.runtime import build_webchat
            _web, _web_built_at = build_webchat(), time.monotonic()
    return _web


def chat_secret_matches(given: str) -> bool:
    expected = os.environ.get("WEB_CHAT_SECRET", "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def secret_matches(given: str) -> bool:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


@app.get("/")
def root():
    return {"ok": True}


@app.post("/telegram")
async def telegram(request: Request, x_telegram_bot_api_secret_token: str = Header("")):
    if not secret_matches(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}                                              # not JSON: nothing to do, and no retry wanted
    try:
        handler = get_handler()
    except Exception as e:  # noqa: BLE001 - config errors name variables, never values; log the type and message
        log.error("handler could not be built: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=503, detail="not ready")        # Telegram retries later; the owner sees it in the logs
    await run_in_threadpool(handler.handle, update)                      # Handler.handle never raises
    return {"ok": True}


@app.post("/ask")
async def ask(request: Request, x_chat_secret: str = Header("")):
    """The Order Flow board's chat panel (through the board's own server, which holds WEB_CHAT_SECRET)."""
    if not chat_secret_matches(x_chat_secret):
        raise HTTPException(status_code=403, detail="forbidden")
    raw = await request.body()
    if len(raw) > MAX_ASK_BYTES:
        raise HTTPException(status_code=413, detail="too large")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="bad request")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad request")
    try:
        chat = get_web()
    except Exception as e:  # noqa: BLE001 - config errors name variables, never values
        log.error("web chat could not be built: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=503, detail="not ready")
    if body.get("reset") is True:
        return JSONResponse(await run_in_threadpool(chat.reset))
    result = await run_in_threadpool(chat.ask, body.get("question"), body.get("tab"))
    return JSONResponse(result, status_code=429 if result.get("limited") else 200)
