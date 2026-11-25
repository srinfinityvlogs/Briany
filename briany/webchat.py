# SPDX-License-Identifier: Apache-2.0
"""The Order Flow chat panel's front door: one question (plus which board tab is open) in, one plain-text answer out.

It reuses Bot.ask and the same per-user limits as Telegram, under one fixed user id (WEB_USER_ID), so the web chat has its
own allowance and cannot use up the Telegram one. Questions about open interest, build-up or levels never reach the model:
Briany has no such data, and the reply says so, tab by tab. Nothing here logs message text.
"""
from __future__ import annotations

import logging
import re

from briany.telegram_bot import MAX_QUESTION_CHARS, RATE_DAY, RATE_MINUTE, TOO_LONG

log = logging.getLogger("briany.webchat")

WEB_USER_ID = 0                                   # Telegram ids are positive, so this never clashes with a real user
TABS = ("nifty50", "fno", "nse", "bse")
TAB_LABELS = {"nifty50": "Nifty 50", "fno": "All F&O", "nse": "All NSE", "bse": "All BSE"}

# Words that mean "futures or options data": the board has it on two tabs, Briany has it on none.
FNO_WORDS = re.compile(
    r"\b(open interest|oi|build[- ]?ups?|option chains?|pcr|max pain|strikes?|futures?|resistance|support (?:level|zone)s?)\b", re.I)

CAN_DO = ("I answer from NSE end-of-day data: why a stock moved, returns, filings, index closes, FII/DII flows and "
          "bulk/block deals.")
FNO_REPLY = {
    "nse": "This tab is price-only: there is no open interest or build-up here, and I don't have that data either. " + CAN_DO,
    "bse": "This tab is price-only: there is no open interest or build-up here, and I don't have that data either. " + CAN_DO,
    "nifty50": "The board shows open interest, build-up and levels for these stocks, but I can't read them yet. " + CAN_DO,
    "fno": "The board shows open interest, build-up and levels for these stocks, but I can't read them yet. " + CAN_DO,
    None: "I don't have open interest, build-up or option-chain data. " + CAN_DO,
}
BSE_NOTE = "All BSE tab: I only cover NSE-listed stocks and NSE indices, so BSE-only stocks and the Sensex are not covered."


def clean_tab(tab) -> str | None:
    return tab if isinstance(tab, str) and tab in TABS else None


class WebChat:
    def __init__(self, bot, state, user_id: int = WEB_USER_ID):
        self.bot, self.state, self.uid = bot, state, user_id

    def ask(self, question, tab=None) -> dict:
        """-> {'ok', 'text', 'note', 'kind', 'limited'}. Never raises."""
        tab = clean_tab(tab)
        text = question.strip() if isinstance(question, str) else ""
        if not text:
            return self._reply(False, "Type a question first.", "fixed")
        if len(text) > MAX_QUESTION_CHARS:
            return self._reply(False, TOO_LONG, "fixed")
        if FNO_WORDS.search(text):
            return self._reply(True, FNO_REPLY[tab], "fixed")                # no model call, no allowance used
        limited = self.state.question_limit(self.uid)
        if limited:
            return self._reply(False, RATE_MINUTE if limited == "minute" else RATE_DAY, "fixed", limited=limited)
        try:
            answer = self.bot.ask(text, self.state.load_context(self.uid))
            self.state.save_context(self.uid, answer.context)
        except Exception as e:  # noqa: BLE001 - Bot.ask never raises; this is a last guard, and it logs the type only
            log.error("web chat failed: %s", type(e).__name__)
            return self._reply(False, "Something went wrong while preparing that answer. Please try again in a moment.", "error")
        route = getattr(answer, "route", None)
        log.info("web answer kind=%s writer=%s tool=%s problems=%d tab=%s", answer.kind, answer.writer,
                 route.tool if route else "-", len(answer.problems), tab or "-")
        return self._reply(answer.kind != "error", answer.text, answer.kind, tab=tab)

    @staticmethod
    def _reply(ok: bool, text: str, kind: str, limited: str = "", tab: str | None = None) -> dict:
        return {"ok": ok, "text": text, "kind": kind, "limited": limited, "note": BSE_NOTE if tab == "bse" and ok else ""}
