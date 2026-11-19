import logging

import fakeredis
import pytest

from briany.bot import HELP, Answer
from briany.router import Context
from briany.telegram_bot import (MAX_QUESTION_CHARS, RATE_DAY, RATE_MINUTE, TOO_LONG, UNKNOWN_COMMAND, TEXT_ONLY,
                                 Handler, State, TelegramApi, TelegramError, parse_command, split_message)
from test_bot import FP, SUMMARY, WHY, bot as real_bot

TOKEN = "123456:SECRET-TOKEN-VALUE"
OWNER, FRIEND, STRANGER = 111, 222, 999


class Wire:
    """Records every call to Telegram; `script` lets a test make chosen calls fail."""

    def __init__(self, script=None):
        self.calls, self.script = [], list(script or [])

    def __call__(self, url, payload, timeout):
        assert TOKEN in url                                       # the token travels only in the URL
        self.calls.append((url.rsplit("/", 1)[1], payload))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        return step or (200, "")

    @property
    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    @property
    def methods(self):
        return [m for m, _ in self.calls]


class SpyBot:
    def __init__(self, text="Acme closed at 110.", footer_html="", symbol="ACME"):
        self.questions, self.text, self.footer_html, self.symbol = [], text, footer_html, symbol

    def ask(self, question, context=None):
        self.questions.append((question, context))
        ctx = Context(self.symbol, "Acme Industries Limited") if self.symbol else (context or Context())
        return Answer(self.text, "move", None, None, "code", (), ctx, body=self.text, footer_html=self.footer_html)


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def update(text, uid=OWNER, chat_type="private", update_id=1, chat_id=None, **extra):
    msg = {"message_id": 1, "from": {"id": uid, "is_bot": False}, "chat": {"id": chat_id or uid, "type": chat_type},
           "text": text}
    msg.update(extra)
    return {"update_id": update_id, "message": msg}


def make(bot=None, allowed=(OWNER, FRIEND), wire=None, clock=None, redis=None, per_minute=3, per_day=40, **kw):
    wire = wire or Wire()
    clock = clock or Clock()
    r = redis if redis is not None else fakeredis.FakeStrictRedis(decode_responses=True)
    state = State(r, clock=clock, per_minute=per_minute, per_day=per_day)
    h = Handler(bot or SpyBot(), TelegramApi(TOKEN, post=wire), state, allowed, **kw)
    return h, wire, r, clock


# ---------------------------------------------------------------- strangers
@pytest.mark.parametrize("text", ["why is acme up", "/start", "/help", "/status", "hello"])
def test_strangers_get_no_reply_and_never_reach_the_bot(text):
    spy = SpyBot()
    h, wire, *_ = make(spy)
    assert h.handle(update(text, uid=STRANGER)) == "stranger"
    assert wire.calls == [] and spy.questions == []


def test_an_empty_allowlist_means_nobody_can_ask():
    spy = SpyBot()
    h, wire, *_ = make(spy, allowed=())
    assert h.handle(update("why is acme up", uid=OWNER)) == "stranger"
    assert wire.calls == [] and spy.questions == []


# ---------------------------------------------------------------- /whoami
def test_whoami_in_a_private_chat_tells_anyone_their_own_id():
    h, wire, *_ = make()
    assert h.handle(update("/whoami", uid=STRANGER, update_id=1)) == "whoami"
    assert h.handle(update("/whoami", uid=OWNER, update_id=2)) == "whoami"
    stranger_msg, owner_msg = (p["text"] for p in wire.sent)
    assert f"Your Telegram user ID is {STRANGER}." in stranger_msg and "ask for access" in stranger_msg
    assert f"Your Telegram user ID is {OWNER}." in owner_msg and "You can use this bot" in owner_msg


@pytest.mark.parametrize("uid", [STRANGER, OWNER])
def test_whoami_is_silent_in_groups_for_everyone(uid):
    h, wire, *_ = make()
    assert h.handle(update("/whoami", uid=uid, chat_type="supergroup", chat_id=-100123)) == "ignored_group"
    assert h.handle(update("/whoami@MyBot", uid=uid, chat_type="group", chat_id=-100123, update_id=2)) == "ignored_group"
    assert wire.calls == []


def test_whoami_can_be_restricted_to_the_allowlist():
    h, wire, *_ = make(whoami_for_strangers=False)
    assert h.handle(update("/whoami", uid=STRANGER)) == "stranger" and wire.calls == []
    assert h.handle(update("/whoami", uid=OWNER, update_id=2)) == "whoami"


def test_a_stranger_repeating_whoami_is_cut_off_silently():
    h, wire, *_ = make()
    outcomes = [h.handle(update("/whoami", uid=STRANGER, update_id=i)) for i in range(1, 9)]
    assert outcomes == ["whoami"] * 5 + ["whoami_limited"] * 3 and len(wire.sent) == 5


# ---------------------------------------------------------------- commands and odd messages
def test_start_and_help_send_the_help_text():
    h, wire, *_ = make()
    assert h.handle(update("/start", update_id=1)) == "help"
    assert h.handle(update("/help@MyBot", update_id=2)) == "help"
    assert all(p["text"] == HELP.replace("&", "&amp;") for p in wire.sent) and len(wire.sent) == 2
    assert all(p["parse_mode"] == "HTML" for p in wire.sent)


def test_unknown_commands_and_non_text_messages_get_a_short_hint():
    h, wire, *_ = make()
    assert h.handle(update("/nope", update_id=1)) == "unknown_command"
    msg = update("", update_id=2)
    del msg["message"]["text"]
    msg["message"]["photo"] = [{"file_id": "x"}]
    assert h.handle(msg) == "not_text"
    assert [p["text"] for p in wire.sent] == [UNKNOWN_COMMAND, TEXT_ONLY]


def test_other_update_types_bots_and_groups_are_ignored():
    spy = SpyBot()
    h, wire, *_ = make(spy)
    assert h.handle({"update_id": 5, "edited_message": {"text": "x"}}) == "ignored"
    assert h.handle({"update_id": 6, "callback_query": {}}) == "ignored"
    assert h.handle("not a dict") == "ignored" and h.handle({}) == "ignored"
    from_bot = update("hi", update_id=7)
    from_bot["message"]["from"]["is_bot"] = True
    assert h.handle(from_bot) == "ignored"
    assert h.handle(update("why is acme up", chat_type="group", chat_id=-5, update_id=8)) == "ignored_group"
    assert wire.calls == [] and spy.questions == []


def test_overlong_questions_are_refused_without_using_the_allowance():
    spy = SpyBot()
    h, wire, r, _ = make(spy)
    assert h.handle(update("x" * (MAX_QUESTION_CHARS + 1))) == "too_long"
    assert wire.sent[0]["text"] == TOO_LONG and spy.questions == [] and r.keys("tg:rl:*") == []


# ---------------------------------------------------------------- questions
def test_a_question_shows_typing_then_the_answer_with_html_links_and_no_link_preview():
    footer = 'Sources: NSE\nLink: <a href="https://a.example/x">Headline (Mint)</a>'
    h, wire, *_ = make(SpyBot("Acme <b>rose</b> & fell.", footer))
    assert h.handle(update("why is acme up today?")) == "answered"
    assert wire.methods == ["sendChatAction", "sendMessage"]
    assert wire.calls[0][1] == {"chat_id": OWNER, "action": "typing"}
    msg = wire.sent[0]
    assert msg["text"] == f"Acme &lt;b&gt;rose&lt;/b&gt; &amp; fell.\n\n{footer}"        # body escaped, footer kept as built
    assert msg["parse_mode"] == "HTML" and msg["link_preview_options"] == {"is_disabled": True}


def test_follow_up_context_is_remembered_per_user_for_the_ttl():
    spy = SpyBot()
    h, wire, r, _ = make(spy)
    h.handle(update("why is acme up today", update_id=1))
    h.handle(update("and last month?", update_id=2))
    h.handle(update("tcs 1 month return", uid=FRIEND, update_id=3))
    assert spy.questions[0][1] == Context() and spy.questions[1][1] == Context("ACME", "Acme Industries Limited")
    assert spy.questions[2][1] == Context()                                           # another user starts fresh
    assert 1700 < r.ttl(f"tg:ctx:{OWNER}") <= 1800


def test_a_context_that_expired_is_gone():
    h, wire, r, _ = make(SpyBot())
    h.handle(update("why is acme up today", update_id=1))
    r.delete(f"tg:ctx:{OWNER}")                                                       # what the TTL does after 30 minutes
    assert h.state.load_context(OWNER) == Context()


def test_questions_without_a_company_do_not_store_a_context():
    h, wire, r, _ = make(SpyBot(symbol=""))
    h.handle(update("hello"))
    assert r.get(f"tg:ctx:{OWNER}") is None


# ---------------------------------------------------------------- rate limits
def test_three_questions_a_minute_then_a_friendly_refusal_then_it_resets():
    spy = SpyBot()
    h, wire, r, clock = make(spy)
    outcomes = [h.handle(update(f"q{i}", update_id=i)) for i in range(1, 6)]
    assert outcomes == ["answered"] * 3 + ["rate_limited_minute"] * 2
    assert wire.sent[-1]["text"] == RATE_MINUTE and len(spy.questions) == 3
    clock.t += 61
    assert h.handle(update("q6", update_id=6)) == "answered"
    assert r.ttl(next(iter(r.keys("tg:rl:m:*")))) <= 120


def test_forty_questions_a_day_and_refusals_do_not_eat_the_daily_allowance():
    clock = Clock(1_700_000_000.0)
    spy = SpyBot()
    h, wire, r, _ = make(spy, clock=clock, per_minute=3, per_day=40)
    n = 0
    for _minute in range(13):                                                         # 13 minutes x 3 answered questions = 39
        for _ in range(3):
            n += 1
            assert h.handle(update("q", update_id=n)) == "answered"
        n += 1
        assert h.handle(update("one too many", update_id=n)) == "rate_limited_minute"
        clock.t += 61
    assert int(r.get(next(iter(r.keys("tg:rl:d:*"))))) == 39                          # the 13 refused attempts were not counted
    assert h.handle(update("q", update_id=900)) == "answered"                         # the 40th
    assert h.handle(update("q", update_id=901)) == "rate_limited_day" and len(spy.questions) == 40


def test_the_daily_limit_message_and_midnight_ist_reset():
    clock = Clock(1_700_000_000.0)                                                    # 2023-11-14 22:13 UTC = 03:43 IST on the 15th
    h, wire, r, _ = make(SpyBot(), clock=clock, per_minute=100, per_day=2)
    outcomes = [h.handle(update("q", update_id=i)) for i in range(1, 4)]
    assert outcomes == ["answered", "answered", "rate_limited_day"] and wire.sent[-1]["text"] == RATE_DAY
    clock.t += 21 * 3600                                                              # past the next midnight IST
    assert h.handle(update("q", update_id=9)) == "answered"


# ---------------------------------------------------------------- Telegram retries
def test_a_retried_update_is_answered_once():
    spy = SpyBot()
    h, wire, *_ = make(spy)
    assert h.handle(update("why is acme up", update_id=42)) == "answered"
    assert h.handle(update("why is acme up", update_id=42)) == "duplicate"
    assert len(spy.questions) == 1 and len(wire.sent) == 1


# ---------------------------------------------------------------- long messages and send failures
def test_a_long_answer_is_split_on_line_breaks_and_stays_under_the_limit():
    footer = 'Link: <a href="https://a.example/x?a=1&amp;b=2">Headline (Mint)</a>'
    text = "\n".join(f"line {i} " + "word " * 40 for i in range(60))
    h, wire, *_ = make(SpyBot(text, footer))
    assert h.handle(update("q")) == "answered"
    assert len(wire.sent) >= 3 and all(len(p["text"]) <= 4096 for p in wire.sent)
    assert sum(p["text"].count("<a href") for p in wire.sent) == 1                    # the tag was never cut in two
    import re
    assert len(re.findall(r"line \d+ ", "".join(p["text"] for p in wire.sent))) == 60   # no line was lost


def test_html_the_server_rejects_is_resent_as_plain_text():
    wire = Wire(script=[(200, ""), (400, "Bad Request: can't parse entities: Unsupported start tag")])
    spy = SpyBot("Acme closed at 110.")
    h, wire, *_ = make(spy, wire=wire)
    assert h.handle(update("q")) == "answered"
    first, retry = wire.sent
    assert first["parse_mode"] == "HTML" and "parse_mode" not in retry and retry["text"] == "Acme closed at 110."


def test_other_telegram_failures_are_logged_not_raised(caplog):
    caplog.set_level(logging.INFO, logger="briany.telegram")
    wire = Wire(script=[(200, ""), (500, "oops")])
    h, wire, *_ = make(SpyBot(), wire=wire)
    assert h.handle(update("q")) == "send_failed"
    assert "outcome=send_failed" in caplog.text and "telegram_status=500" in caplog.text


def test_a_failing_http_library_cannot_leak_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    boom = Wire(script=[(200, ""), RuntimeError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage timed out")])
    h, wire, *_ = make(SpyBot(), wire=boom)
    assert h.handle(update("q")) == "send_failed"
    assert TOKEN not in caplog.text
    failing = Wire(script=[RuntimeError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage timed out")])
    with pytest.raises(TelegramError) as e:
        TelegramApi(TOKEN, post=failing).send_message(1, "x")
    assert TOKEN not in str(e.value) and e.value.__cause__ is None


def test_handle_never_raises_even_if_the_bot_does(caplog):
    caplog.set_level(logging.INFO, logger="briany.telegram")

    class Exploding:
        def ask(self, *a, **k):
            raise RuntimeError("secret internal detail")
    h, wire, *_ = make(Exploding())
    assert h.handle(update("q")) == "error"
    assert "error=RuntimeError" in caplog.text and "secret internal detail" not in caplog.text


# ---------------------------------------------------------------- Redis trouble
def test_if_redis_fails_an_allowed_user_still_gets_an_answer():
    class Broken:
        def __getattr__(self, name):
            raise ConnectionError("redis down")
    spy = SpyBot()
    h, wire, *_ = make(spy, redis=Broken())
    assert h.handle(update("why is acme up")) == "answered" and len(spy.questions) == 1
    assert h.handle(update("/whoami", uid=STRANGER, update_id=2)) == "whoami"


# ---------------------------------------------------------------- logging
def test_the_log_line_has_ids_route_writer_and_timing_but_no_message_text_or_token(caplog):
    caplog.set_level(logging.INFO, logger="briany.telegram")
    from briany.llm import ToolCall                       # noqa: F401  (route comes from FP below)
    h, wire, *_ = make(real_bot([FP("groq", WHY, [SUMMARY])]))
    assert h.handle(update("why is acme up today", update_id=77)) == "answered"
    line = next(r.getMessage() for r in caplog.records if "outcome=answered" in r.getMessage())
    assert "update=77" in line and "tool=why_moved" in line and "writer=llm:groq" in line and "ms=" in line
    assert "acme up today" not in caplog.text and TOKEN not in caplog.text


def test_rejected_model_text_is_logged_as_problems(caplog):
    from test_bot import BAD_CAUSAL
    caplog.set_level(logging.INFO, logger="briany.telegram")
    h, wire, *_ = make(real_bot([FP("groq", WHY, [BAD_CAUSAL]), FP("gemini", WHY, [BAD_CAUSAL])]))
    h.handle(update("why is acme up today"))
    assert "writer=fallback" in caplog.text and "problems=2" in caplog.text and "causal" in caplog.text


# ---------------------------------------------------------------- with the real Bot, end to end
def test_a_real_answer_goes_out_as_valid_html_with_clickable_sources():
    h, wire, *_ = make(real_bot([FP("groq", WHY, [SUMMARY])]))
    assert h.handle(update("why is acme up today")) == "answered"
    text = "\n".join(p["text"] for p in wire.sent)
    assert "Acme Industries Limited (ACME) closed at" in text and "Possible contributing factors" in text
    assert '<a href="https://news/1">' in text and "Sources: " in text and "Information only" in text
    assert "<b>" not in text and "&lt;" not in text.replace("&lt;b", "")             # nothing unintended escaped or injected


# ---------------------------------------------------------------- helpers
def test_parse_command():
    assert parse_command("/Help") == "help" and parse_command("/help@MyBot now") == "help"
    assert parse_command("why /help") is None and parse_command("") is None


def test_split_message_edge_cases():
    assert split_message("") == [] and split_message("short") == ["short"]
    parts = split_message("a" * 9000, limit=4000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    spaced = split_message(("word " * 2000).strip(), limit=4000)
    assert all(len(p) <= 4000 for p in spaced) and " ".join(spaced).split() == ["word"] * 2000
    assert split_message("one\n\ntwo") == ["one\n\ntwo"]


def test_the_log_line_names_the_region_vercel_runs_the_function_in(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="briany.telegram")
    monkeypatch.setenv("VERCEL_REGION", "bom1")
    h, wire, *_ = make()
    h.handle(update("/help", update_id=5))
    assert "outcome=help" in caplog.text and "region=bom1" in caplog.text
    monkeypatch.delenv("VERCEL_REGION")
    h.handle(update("/help", update_id=6))
    assert "region=-" in caplog.text
