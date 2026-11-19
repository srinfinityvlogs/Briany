# Briany

A personal Telegram bot that answers plain-English questions about NSE-listed stocks and ETFs, using official
end-of-day data. It is built to say what the data shows and to admit what it doesn't know.

> **Information only, not investment advice.** Prices are NSE official end-of-day closes, not live quotes.
> The bot never predicts, recommends or states a confirmed cause for a price move.

## What you can ask

One stock or ETF at a time, in your own words:

- **Why it moved:** "why is Tata Steel up today?", "why did Infosys fall on 3 Nov?"
- **Returns:** "TCS 6 month return" (1 week, 1 month, 3 months, 6 months, 1 year or year to date).
- **Filings and dates:** "any announcements from Infosys?"
- Follow-ups such as "and last month?" reuse the previous company for 30 minutes.

Answers are 5 to 10 short lines with sources and an "as of" time. Commands: `/help`, `/whoami` (private chats only:
shows your Telegram ID, needed to be added to the allowlist).

## Principles

1. **Code builds every fact and number.** The model only routes the question and, for "why did it move", phrases the
   opening sentence. A validator rejects model text containing a number, date, name or causal claim that is not in the
   verified data, and the bot then falls back to a text written by code.
2. **Possible contributing factors, never causes.** Filings, board meetings, corporate actions and news are listed with
   their timing relative to the market close.
3. **A failed source is a gap, not "nothing found".** "No clear evidence" is said only when every source was checked.
4. **Split and bonus days are not guessed.** NSE's previous close is not adjusted for them, so the percentage is withheld.
   A one-day move of 30% or more with no listed action makes returns for that period refuse.
5. **Strangers get silence.** Only Telegram IDs on the allowlist receive answers.

## How it works

```
Telegram ──> Vercel function (app.py, Mumbai) ──> router ──> evidence assembler ──> writer ──> validator ──> reply
                   │                                          │
                   │                            Upstash Redis │ NSE APIs, Google News RSS (live, cached)
                   │                    prices, actions, names│
GitHub Actions (ingest-prices, evenings) ──> NSE end-of-day files ──┘
```

- **Hosting:** the bot is a webhook on Vercel; the nightly data load runs in GitHub Actions; storage is Redis.
  Nothing runs on your own computer.
- **Models:** Groq (`openai/gpt-oss-120b`) first, Gemini as a fallback. If no model is reachable or its text is
  rejected, the code-built answer is still sent.

## Repository map

| Path | What it is |
|---|---|
| `app.py` | Vercel entry point: `POST /telegram` (checks the webhook secret) and a bare `GET /` |
| `briany/telegram_bot.py` | Allowlist, rate limits, per-user context, message splitting, HTML sending |
| `briany/runtime.py` | Builds everything from environment variables |
| `briany/bot.py` | The pipeline from question to message |
| `briany/router.py`, `llm.py` | Question routing (rules plus Groq/Gemini) and the provider chain |
| `briany/evidence.py` | Turns a question into a verified packet of facts, context, factors and gaps |
| `briany/answer.py` | Code-built text, the validator, and the footer with sources and links |
| `briany/marketdata.py`, `nsefiles.py`, `ingest.py`, `store.py` | End-of-day prices: download, parse, store |
| `briany/nse.py`, `nse_parsers.py`, `refresh_actions.py` | NSE filings, board meetings, calendar, corporate actions |
| `briany/news.py` | Google News RSS with relevance and age filters |
| `briany/symbols.py` | Turns "reliance" or "tcs" into an NSE symbol, or asks which one |
| `briany/snapshot.py`, `capture_cli.py` | Record real data once and replay it offline for regression tests |
| `*_cli.py`, `dbcheck.py`, `crosscheck.py`, `*_samples.py` | Small tools run from GitHub Actions |
| `tests/` | The test suite (see below) |

## Run your own

Everything below uses free tiers at the time of writing. Check each provider's current limits before relying on them.

1. **Telegram:** talk to `@BotFather`, create a bot, keep the token.
2. **Model keys:** a [Groq](https://console.groq.com) API key (required) and a Gemini API key (optional fallback).
3. **GitHub:** fork this repository.
4. **Vercel:** import the fork. Leave Root Directory empty (the repository root); Vercel finds `app.py`.
   Add an Upstash Redis database from Vercel's Storage tab: it provides `KV_REDIS_URL`.
5. **Vercel environment variables** (Settings → Environment Variables, Production): see `.env.example` for the full list.
   Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` (any random string, for example `openssl rand -hex 24`),
   `GROQ_API_KEY`, and the Redis URL. Recommended: `ALLOWED_TELEGRAM_IDS`. Optional: `GEMINI_API_KEY` and the tuning values.
6. **GitHub Actions secrets** (Settings → Secrets and variables → Actions): `REDIS_URL` (the same `rediss://` URL),
   `GROQ_API_KEY`, and `GEMINI_API_KEY` if you use it. These are only for the data load and the tools.
7. **Load the data:** in Actions run `ingest-prices` with `refresh_instruments = yes`, then again with `start` set to a
   date about a year back and `end` to yesterday (run it in chunks if a run hits the 45-minute limit), then once with
   no inputs, which also refreshes the corporate-actions copy. Answers refuse prices if that copy is more than 3 days
   old. After that the schedule keeps it current (about 20:00 IST on weekdays, with a safety run at 03:00 IST).
   Check with `ingest-prices` and `check = yes`.
8. **Register the webhook** (run once; the token and secret stay in your own shell):
   ```
   curl -s "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     --data-urlencode "url=https://<your-project>.vercel.app/telegram" \
     --data-urlencode "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
   ```
   `getWebhookInfo` on the same API shows the last error, if any.
9. **Add yourself:** send `/whoami` to your bot in a private chat, put the number in `ALLOWED_TELEGRAM_IDS`
   (comma-separated for friends), and redeploy. Until then the bot stays silent.

Defaults: 3 questions a minute and 40 a day per user (`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_DAY`).

## Operations (GitHub Actions, run by hand unless noted)

| Workflow | Purpose |
|---|---|
| `ingest-prices` | Scheduled daily load of end-of-day files and corporate actions; also backfill and read-only checks |
| `ask-bot` | Ask the full bot a question on real data and read the result in the log |
| `assemble-evidence` | Print the verified packet (before any model) for one question |
| `route-test` | Check how the question router behaves on a built-in set of questions |
| `crosscheck-actions` | Check that large one-day moves match NSE's corporate-action list |
| `capture-scenarios` | Record real scenarios for the regression tests (see below) |
| `nse-samples`, `news-samples` | Capture real feed samples to verify the parsers |

On Vercel, Runtime Logs show one line per update: update id, outcome, region, route, writer, problems and timing.
Message text, tokens and the Redis URL are never logged.

## Tests

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite uses fake Redis and fake network clients. `tests/test_phase1_scenarios.py` replays a recording of real NSE data
(Reliance, TCS, Infosys, Tempsens, KRN and GoldBees on three days) and checks 60 answers three ways: against the recorded
packets, against arithmetic recomputed from the raw prices, and against the safety rules. If an answer is *meant* to
change, read the failing diff and accept it with:

```
python -m briany.snapshot tests/fixtures/phase1/phase1_scenarios.json.gz
```

To record new scenarios, run `capture-scenarios` in Actions and replace the fixture with the downloaded file.

## Known limits

- One stock or ETF per question. No comparisons, index levels, intraday prices, bulk-deal or market-wide questions,
  result figures, or US markets yet.
- Returns and events show NSE's short price-file name (for example `TATA CONSULTANCY SERV LT`); "why" answers use the full
  name from filings.
- News covers about the last week, so older days say news was not checked. Headlines that say only "Reliance" or "RIL"
  are not matched to Reliance Industries (precision over recall).
- FII/DII data appears only when the day asked about is the latest stored day.
- The evidence window assumes a 15:30 close, also on special weekend sessions.
- Bond-ETF maturity notices are not stored.
- NSE's public APIs are unofficial and can change or block requests; the client fails loudly rather than guess.

## Data sources and terms

NSE end-of-day files and public APIs; Google News RSS (its notice limits it to personal, non-commercial use). This
project is for personal use. Respect each source's terms if you run your own copy.

## Licence

Apache-2.0. See `LICENSE`. Each Python file carries an SPDX identifier.
