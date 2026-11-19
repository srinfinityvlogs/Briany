"""Telegram front end: one webhook update in, zero or more messages out. No web framework in here.

Flow for a message: dedupe -> /whoami (private chats only) -> allowlist (strangers get nothing) ->
commands -> rate limit -> Bot.ask -> send. The bot is a personal one: questions are answered in private chats only.
Nothing here logs message text, tokens or the Redis URL.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from html import escape

from briany.bot import HELP
from briany.evidence import IST
from briany.router import Context

log = logging.getLogger("briany.telegram")

MESSAGE_LIMIT = 4000            # Telegram's hard limit is 4096 characters
MAX_QUESTION_CHARS = 500
WHOAMI_PER_MINUTE = 5           # protects the Telegram API from a stranger who repeats /whoami

TOO_LONG = f"Please keep your question under {MAX_QUESTION_CHARS} characters."
UNKNOWN_COMMAND = "I don't know that command. Send /help to see what I can do."
TEXT_ONLY = "I can only read text messages. Send /help to see what I can do."
RATE_MINUTE = "That's a lot of questions at once. Please wait a minute and ask again."
RATE_DAY = "You've reached today's question limit. It resets at midnight IST."


class TelegramError(RuntimeError):
    """A failed Telegram call. Carries the status and Telegram's short description, never the URL (it holds the token)."""

    def __init__(self, status: int, description: str = ""):
        super().__init__(f"telegram status {status}")
        self.status, self.description = status, description

    @property
    def is_parse_error(self) -> bool:
        return self.status == 400 and "parse" in self.description.lower()


def default_post(url: str, payload: dict, timeout: float):
    import requests
    r = requests.post(url, json=payload, timeout=timeout)
    try:
        description = str(r.json().get("description", ""))[:200]
    except ValueError:
        description = ""
    return r.status_code, description


class TelegramApi:
    def __init__(self, token: str, post=default_post, timeout: float = 10.0):
        self._token, self._post, self.timeout = token, post, timeout

    def _call(self, method: str, payload: dict) -> None:
        try:
            status, description = self._post(f"https://api.telegram.org/bot{self._token}/{method}", payload, self.timeout)
        except Exception as e:  # noqa: BLE001 - library errors contain the URL, which contains the token
            raise TelegramError(0, type(e).__name__) from None
        if status != 200:
            raise TelegramError(status, description)

    def send_message(self, chat_id: int, text: str, html: bool = True) -> None:
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if html:
            payload["parse_mode"] = "HTML"
        self._call("sendMessage", payload)

    def send_typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except TelegramError:
            pass                                    # a missing typing indicator is not worth failing for


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut at line breaks so an HTML tag (always on one line) is never split; a single huge line is cut at a space."""
    out, cur = [], ""

    def flush():
        nonlocal cur
        if cur.strip():
            out.append(cur.strip("\n"))
        cur = ""

    for line in text.strip().split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            flush()
            out.append(line[:cut])
            line = line[cut:].lstrip()
        if cur and len(cur) + 1 + len(line) > limit:
            flush()
        cur = f"{cur}\n{line}" if cur else line
    flush()
    return out


def parse_command(text: str):
    """'/help@MyBot now' -> 'help'. None when the text is not a command."""
    if not text.startswith("/"):
        return None
    return text.split(None, 1)[0][1:].split("@", 1)[0].lower()


class State:
    """Per-user context, rate limits and update dedupe in Redis. A Redis error never stops the bot: it is logged by type
    and the safe default is used (no context, not a duplicate, not limited); the allowlist still bounds the load."""

    def __init__(self, r, clock=time.time, context_ttl=1800, per_minute=3, per_day=40):
        self.r, self._clock = r, clock
        self.context_ttl, self.per_minute, self.per_day = context_ttl, per_minute, per_day

    def seen_update(self, update_id) -> bool:
        try:
            return not self.r.set(f"tg:upd:{update_id}", "1", nx=True, ex=600)
        except Exception as e:  # noqa: BLE001
            log.warning("redis dedupe failed: %s", type(e).__name__)
            return False

    def load_context(self, uid: int) -> Context:
        try:
            raw = self.r.get(f"tg:ctx:{uid}")
            data = json.loads(raw) if raw else {}
            return Context(str(data.get("s", "")), str(data.get("n", "")))
        except Exception as e:  # noqa: BLE001
            log.warning("redis context load failed: %s", type(e).__name__)
            return Context()

    def save_context(self, uid: int, ctx: Context) -> None:
        try:
            if ctx.last_symbol:
                self.r.set(f"tg:ctx:{uid}", json.dumps({"s": ctx.last_symbol, "n": ctx.last_name}), ex=self.context_ttl)
        except Exception as e:  # noqa: BLE001
            log.warning("redis context save failed: %s", type(e).__name__)

    def _over(self, name: str, uid: int, window: str, limit: int, ttl: int) -> bool:
        key = f"tg:rl:{name}:{uid}:{window}"
        pipe = self.r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl)                       # set together with the count, so a key can never be left without expiry
        return pipe.execute()[0] > limit

    def question_limit(self, uid: int):
        """None if allowed, else 'minute' or 'day'. A refused attempt does not use up the daily allowance."""
        try:
            now = self._clock()
            if self._over("m", uid, str(int(now // 60)), self.per_minute, 120):
                return "minute"
            if self._over("d", uid, datetime.fromtimestamp(now, IST).date().isoformat(), self.per_day, 172800):
                return "day"
        except Exception as e:  # noqa: BLE001
            log.warning("redis rate limit failed: %s", type(e).__name__)
        return None

    def whoami_limited(self, uid: int) -> bool:
        try:
            return self._over("w", uid, str(int(self._clock() // 60)), WHOAMI_PER_MINUTE, 120)
        except Exception as e:  # noqa: BLE001
            log.warning("redis rate limit failed: %s", type(e).__name__)
            return False


class Handler:
    def __init__(self, bot, api: TelegramApi, state: State, allowed_ids, whoami_for_strangers: bool = True,
                 clock=time.monotonic):
        self.bot, self.api, self.state = bot, api, state
        self.allowed, self.whoami_for_strangers, self._clock = frozenset(allowed_ids), whoami_for_strangers, clock

    # ---------------------------------------------------------------- entry point
    def handle(self, update) -> str:
        """Process one update. Never raises; returns a short outcome label (also logged)."""
        t0 = self._clock()
        update_id = update.get("update_id") if isinstance(update, dict) else None
        detail = {}
        try:
            outcome = self._handle(update, detail)
        except TelegramError as e:
            outcome = "send_failed"
            detail["telegram_status"] = e.status
        except Exception as e:  # noqa: BLE001 - log the type only (no secrets, no message text)
            outcome = "error"
            detail["error"] = type(e).__name__
        extras = " ".join(f"{k}={v}" for k, v in detail.items())
        log.info("update=%s outcome=%s ms=%d %s", update_id, outcome, int((self._clock() - t0) * 1000), extras)
        return outcome

    # ---------------------------------------------------------------- the logic
    def _handle(self, update, detail: dict) -> str:
        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            return "ignored"                                                  # edits, callbacks, channel posts, ...
        sender, chat = msg.get("from") or {}, msg.get("chat") or {}
        uid, chat_id = sender.get("id"), chat.get("id")
        if not isinstance(uid, int) or not isinstance(chat_id, int) or sender.get("is_bot"):
            return "ignored"
        private = chat.get("type") == "private"
        update_id = update.get("update_id")
        if isinstance(update_id, int) and self.state.seen_update(update_id):
            return "duplicate"
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        text = text.strip()
        command = parse_command(text)
        allowed = uid in self.allowed

        if command == "whoami":
            if not private:
                return "ignored_group"
            if not (allowed or self.whoami_for_strangers):
                return "stranger"
            if self.state.whoami_limited(uid):
                return "whoami_limited"
            note = ("You can use this bot." if allowed
                    else "Send this number to the owner of this bot to ask for access.")
            self._say(chat_id, f"Your Telegram user ID is {uid}.\n{note}")
            return "whoami"

        if not allowed:
            return "stranger"                                                 # silent: no reply, nothing revealed
        if not private:
            return "ignored_group"
        if command in ("start", "help"):
            self._say(chat_id, HELP)
            return "help"
        if command is not None:
            self._say(chat_id, UNKNOWN_COMMAND)
            return "unknown_command"
        if not text:
            self._say(chat_id, TEXT_ONLY)
            return "not_text"
        if len(text) > MAX_QUESTION_CHARS:
            self._say(chat_id, TOO_LONG)
            return "too_long"
        limited = self.state.question_limit(uid)
        if limited:
            self._say(chat_id, RATE_MINUTE if limited == "minute" else RATE_DAY)
            return f"rate_limited_{limited}"

        self.api.send_typing(chat_id)
        answer = self.bot.ask(text, self.state.load_context(uid))
        self.state.save_context(uid, answer.context)
        route = getattr(answer, "route", None)
        detail.update(tool=route.tool if route else "-", writer=answer.writer, kind=answer.kind,
                      problems=len(answer.problems))
        if answer.problems:
            detail["problem_notes"] = "|".join(str(p)[:80] for p in answer.problems[:3]).replace(" ", "_")
        self._send_answer(chat_id, answer)
        return "answered"

    # ---------------------------------------------------------------- sending
    def _say(self, chat_id: int, plain_text: str) -> None:
        self.api.send_message(chat_id, escape(plain_text, quote=False), html=True)

    def _send_answer(self, chat_id: int, answer) -> None:
        sent = 0
        try:
            for chunk in split_message(answer.telegram_html):
                self.api.send_message(chat_id, chunk, html=True)
                sent += 1
        except TelegramError as e:
            if not e.is_parse_error or sent:
                raise
            log.warning("telegram rejected the HTML; sending plain text")
            for chunk in split_message(answer.text):                         # plain text carries no tags to break
                self.api.send_message(chat_id, chunk, html=False)
