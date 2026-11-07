# Briany

Personal Indian stock market chatbot (NSE first, Telegram interface). Work in progress.

Principles: code builds every fact and number; the LLM only routes questions and
phrases the answer; no data means "no clear evidence found", never a guessed cause.

Setup: copy `.env.example` to `.env`, fill it in, then `pip install -r requirements-dev.txt && pytest`.
Licence: Apache-2.0 (see LICENSE).
