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
| `briany/names.py`, `refresh_names.py` | NSE's full company names, stored daily so a stock has one name in every answer |
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
   no inputs, which also refreshes the corporate-actions copy. Then run `refresh-names` once (it keeps itself current
   after that, on weekdays around 12:00 IST). Answers refuse prices if that copy is more than 3 days
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
| `ingest-flows` | Scheduled at 19:47, 21:13, 03:53 and 11:17 IST (never on the hour: GitHub delays or drops scheduled runs at the start of an hour), and also as a last step of `ingest-prices`'s scheduled runs: store the day's FII/DII flows and bulk, block and short-selling deals in Redis (`flz:*`), about 3 KB a day. NSE sometimes answers a GitHub runner with HTTP 403 (seen on 24 Nov 2026); this job waits 30 s and then 90 s and asks again, and its several daily chances cover the rest. NSE gives the latest day only, so a day not stored is lost. Run with `check = yes` for a read-only report |
| `verify-flows` | Scheduled daily at 12:07 IST Tue-Sat: one more attempt to store the latest FII/DII and deals day, then a read-only check that every weekday trading day since storage began has FII/DII, bulk and block records. The run turns red (and GitHub can email you) only when a day is missing that is not a known gap; a missing day cannot be rebuilt once NSE shows a newer one, so run `ingest-flows` at once. Weekend sessions and days in the `SPECIAL_SESSION_DAYS` repository variable are skipped. Days lost for good are listed in `KNOWN_GAPS` in `briany/flows.py` (24 Nov 2026, when NSE blocked the runners) and can be added without a code change through the `KNOWN_FLOW_GAPS` repository variable (comma-separated dates); they are named in the report as known gaps and not counted |
| `ask-bot` | Ask the full bot a question on real data and read the result in the log |
| `assemble-evidence` | Print the verified packet (before any model) for one question (modes: why, return, events, index) |
| `route-test` | Check how the question router behaves on a built-in set of questions |
| `crosscheck-actions` | Check that large one-day moves match NSE's corporate-action list |
| `refresh-names` | Scheduled daily: store NSE's full company names (cosmetic; a failure never affects prices or answers) |
| `capture-scenarios` | Record real scenarios for the regression tests (see below) |
| `name-samples` | Capture NSE's name lists and measure how many instruments each one names |
| `draft-check` | Ask each model to write the summary for real packets and show what the validator rejects and why |
| `flow-samples` | Capture the candidate FII/DII and deal sources (used once to choose the storage design) |
| `index-samples` | Capture NSE's index end-of-day file for one or more days (comma separated), to check older dates and holidays |
| `pin-dependencies` | Resolve exact versions of the runtime packages for Vercel (see Updating dependencies) |
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

## Updating dependencies

`requirements.in` lists the runtime packages a person edits. Run `pin-dependencies` in Actions, download
`requirements.pinned.txt`, and commit its contents as `requirements.txt`, which is what Vercel installs. Pinned versions mean
a new release of a library can never change a deployment on its own.

## Known limits

- One stock, ETF or NSE index per question, except a return comparison of exactly two. No intraday prices or other market-wide questions,
  result figures, or US markets yet.
- Listing days: a stock that is in none of the price files of the previous 20 stored trading days is treated as possibly newly
  listed (or back after a long gap). NSE's previous-close field is then an issue or reference price, so a "why did it move"
  answer gives the close and that reference price, says it may be its first trading day, and shows no percentage. One missing
  day does not trigger this (NSE's file lists only scrips that traded, so illiquid stocks skip days), and with fewer than 20
  earlier days stored nothing is claimed. A corporate action on the day takes precedence.
- FII/DII and deals (from `ingest-flows`, stored daily, see the workflow table): "FII DII today", "what did FIIs do on 20 Nov",
  "FII flows this week / last month", "bulk deals in TCS", "block deals today", "short selling today". Flows are NSE's
  market-wide FII/FPI and DII buying, selling and net in Rs crore; a period is added up only when every trading day of it is
  stored, otherwise the stored days are listed and no total is given. History exists only from the day storage began (NSE
  publishes the latest day only), so "last month" fills in over a month. Deals come from the stored days: one stock's deals
  (the latest six, with the count of all matches), or a market-wide day with the counts and the largest by value (quantity
  times price, computed here). Short-selling rows are dated a day behind bulk and block, and the same trade can appear in both
  the bulk and block lists; the answer says so. No FII/DII figure for one stock, and no comparison of flows or deals.
- Comparisons: two stocks, ETFs or NSE indices (mixed is fine) over one period (1w, 1m, 3m, 6m, 1y, ytd), for example
  "compare TCS and Infosys over 1 year" or "nifty bank vs nifty it ytd". Each side is its own return, with every refusal rule of
  a single return, and both use the same start and end days. The bot states each change, which is higher and by how many
  percentage points (computed in code), and the closes behind them. If either side cannot be given (a split or bonus in the
  period, an index that did not exist at the start, a missing file, VIX) nothing is ranked: the reason is stated and the
  other side's figure is shown on its own. No comparison of a single day's moves, of more than two, or without a period (the
  bot asks for one). An ambiguous name gets the usual "Did you mean" question. A comparison does not change what "and
  yesterday?" refers to.
- Indices (NSE only): the day's close, change, open/high/low and NSE's published P/E, P/B and dividend yield, and the change
  in level over 1w, 1m, 3m, 6m, 1y or ytd. All of it comes from NSE's end-of-day index file, fetched on demand and cached for
  an hour when it is today's or yesterday's file and for a week when older (a failure is never cached; nothing is stored per
  day); the bot never explains why an index moved (it says so in one line) and does not
  cover BSE indices such as the Sensex. A return reads two files (the start day is the last stored trading day on or before
  the target, within 7 days, as for stocks) and is refused when either file is missing, when the index did not exist at the
  start (about a quarter of the 169 indices are newer than Dec 2025), for India VIX and Nifty50 Dividend Points (a
  percentage change is meaningless there), or when a row's figures disagree. It is the change in the index level, without
  dividends unless the index is itself a total-return index. The 169 index names come from the 20 Nov 2026 file, so an index
  NSE adds later is not recognised until `briany/index_names.py` is updated. A missing or odd file is reported as "could not
  be fetched", never as another day's figures. For a day without a file NSE has answered both HTTP 404 and HTTP 200 with an
  HTML error page, so the file's content is checked, not the status code.
- Stocks are called by NSE's full company name everywhere. ETFs and SME stocks keep the short, truncated name from the
  price file (for example `NIP IND ETF GOLD BEES`): on 20 Nov 2026 NSE's ETF list had no proper-name column and its SME
  list held a single row. Run `name-samples` again to see whether that has changed.
- News covers about the last week, so older days say news was not checked. Headlines that say only "Reliance" or "RIL"
  are not matched to Reliance Industries (precision over recall).
- FII/DII data appears only when the day asked about is the latest stored day.
- Which index close is "today": the latest stored trading day. If NSE's file for that day is not out yet, the bot uses the
  previous stored trading day and says so ("not available yet, so this is the previous trading day"); a day the user named is
  never swapped. On a weekday after the 09:15 open, when the newest stored close is from an earlier day, the answer adds "No
  close for today is stored yet (the market may still be open, closed for a holiday, or NSE's file may not be out yet), so this
  is the last stored close, <date>". There is no live or intraday index data, so "why is the Nifty down" during the session
  shows the last completed close, the note, and the line that index moves are not explained.
- Special sessions: no source here gives the hours of a special session (a Muhurat or Budget day, an early close), and none are
  typed in by hand. A weekend day, or a weekday listed in the `SPECIAL_SESSION_DAYS` environment variable (comma-separated ISO
  dates, set it in Vercel when NSE announces one), is treated as ambiguous: filings and news published on that calendar day are
  neither placed before nor after its close and are not listed, for the move on that day and for the next trading day, and the
  answer names the day ("special trading session"). It never says "no clear evidence" for such a day. A weekday special session
  that is not in the list is treated as a normal day.
- Only the opening summary of a "why did it move" answer is model-written (`WRITER_KINDS` in `briany/bot.py`). Every other
  answer (returns, indices, comparisons, flows, deals) is built by code: the code text is already the finished answer, so a
  model would only reword it, at the cost of latency, free-tier rate limits and a new way to go wrong.
- Bond-ETF maturity notices are not stored.
- NSE's public APIs are unofficial and can change or block requests; the client fails loudly rather than guess.

## Data sources and terms

NSE end-of-day files and public APIs; Google News RSS (its notice limits it to personal, non-commercial use). This
project is for personal use. Respect each source's terms if you run your own copy.

## Licence

Apache-2.0. See `LICENSE`. Each Python file carries an SPDX identifier.
