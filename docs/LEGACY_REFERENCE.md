# Historical reference — original v0.1 manual

This document is preserved for detailed CLI/provider background. Its behavior and safety claims can be outdated. Current README, ARCHITECTURE, LEARNING and MIGRATION documents take precedence.

# qmag — systematic Qullamaggie-style momentum trading

`qmag` turns Kristjan Kullamägi's (Qullamaggie) publicly described swing-trading
playbook into code that **scans the whole US market**, runs every context check
on each setup — market regime, sector/theme momentum, news, social and
options-flow sentiment, earnings dates, float and short interest —
**sizes the trade with an entry, stop and profit-taking plan**, **writes down
its justification**, **draws the chart**, and **trades it** — paper or live —
on Alpaca, Interactive Brokers or MetaTrader 5, fully automated around the
clock, with a browser dashboard to watch and interrogate it.

**Contents:** [Rules](#the-rules-being-implemented) · [Quick start](#quick-start) ·
[Handover: run it, operate it, take it over](#handover-run-it-operate-it-take-it-over) ·
[Pipeline](#from-signal-to-order-the-pipeline) · [Sentiment & themes](#sentiment-and-theme-momentum) ·
[Live context](#live-context-news-social-options-flow-events) · [Optimisation](#walk-forward-optimisation) ·
[Paper / live trading](#paper--live-trading) · [Operating controls](#operating-controls-kill-switch-alerts-api-budget-remote-access) ·
[Data integrity](#data-integrity-nothing-is-made-up) · [Layout](#layout) · [Roadmap](#roadmap-ideas)

| Piece | What it does |
| --- | --- |
| `qmag universe build` | Pulls every NASDAQ/NYSE/AMEX common stock (~5,600), keeps the liquid ones (~2,900) and caches sector / industry / float / short interest for each — no hand-kept ticker list. |
| `qmag scan` | Tonight's watchlist: momentum leaders in tight flags near their pivot, plus anything that triggered on the latest bar (breakouts, episodic pivots). |
| `qmag chart SYMBOL` | Annotated chart: flag box, pivot, sized entry, stop, ⅓-off target, MAs. |
| `qmag flow SYMBOL` | Scans one ticker's options tape for unusual trades via the Unusual Whales API (on by default when `UNUSUAL_WHALES_API_KEY` is set; `--no-options-flow` turns it off). |
| `qmag edge SYMBOL` | The **Unusual Whales edge score**: 22 independent reads (dealer gamma, net premium, OI build, skew, dark pool, short interest, insiders, 13F, congress, analysts, seasonality, market and sector tide, ...) each scored −1..+1, weight-averaged, with coverage, and compared with the entry threshold the trader enforces. `--rules` prints how every sub-score is derived. |
| `qmag review SYMBOL` | Desk-checks one ticker and asks the optional LLM reviewer (Gemini or any OpenAI-compatible model) for its strict-JSON BUY/SELL/HOLD verdict on the full edge bundle. |
| `qmag backtest` | Portfolio-level daily backtest with his sizing and exit rules; writes `trades.csv` / `equity.csv`. |
| `qmag optimize` | Walk-forward grid search — optimises in-sample, then reports how the pick did out-of-sample so you can tell signal from curve-fit. |
| `qmag paper run` | One trading cycle: reconcile, manage exits, gather context, check + size + justify + chart new setups, place bracket / OCO orders. Paper ledger, Alpaca, IBKR or MT5. |
| `qmag daemon` | Fully automated 24/7 mode on the NYSE clock, tiered: nightly whole-universe scan arms a shortlist, pre-market gap screen, focused passes every 5 min, movers sweeps, Saturday unusual-options (insider) scan, Sunday universe rebuild. |
| `qmag insider-scan` | The Saturday job on demand: flag the week's most unusual options trades and have the AI explain the likely catalyst. |
| `qmag learn` | Review the trade journal and the shadow ledger: what worked, what did not and why, and (bounded, evidence-backed) tune the selection and trigger knobs. |
| `qmag dashboard` | Web UI: regime, theme leaderboard, every plan as a card with chart, checklist, sentiment gauges and a written justification; plan detail pages; on-demand analysis of any ticker; positions, orders, trade journal; a `/status` page for every connection. |
| `qmag status` | Every data / broker / context / model connection: enabled, configured, last OK, latency, last error. `--probe` tests them all now. Nothing is ever substituted for missing data — this is where you see what was missing. |
| `qmag halt` / `qmag resume` | The kill switch: stop every new entry now (daemon, dashboard and CLI all honour it; open positions keep their stops, targets and trails), optionally `--flatten` everything at market. `resume` clears it. |
| `qmag alert-test` | Send a test push alert over the configured Telegram bot / webhook and show what answered. |
| `qmag alert TITLE [BODY] [--level warn]` | Push one alert from a script or cron job over the same channels (the tunnel watcher uses it). |
| `qmag broker-test` | Prove order routing end to end: a far-away 1-share bracket placed, listed and cancelled (`--kind bracket`, safe on live), or a 1-share buy-and-flatten round trip (`--kind fill`, paper unless `--allow-live`). Every step with its latency; exit 1 on failure. |

> This is research tooling, not a money printer. Nothing here is investment
> advice, and there is no "best" version of a discretionary method — see
> [What is (and is not) automatable](#what-is-and-is-not-automatable).

## The rules being implemented

> Looking for the step-by-step account of how a stock goes from the whole
> market to one order, and which setting moves each step? Read
> [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

**Universe / momentum filter** — stock is up ≥30 % in 1 month, ≥50 % in 3 months
or ≥100 % in 6 months, ADR ≥ 3.5 %, liquid (≥ $5 M/day), price ≥ $3.

**Breakout** — after the impulse, a 10–60 day flag that retraces less than half
of the move, whose range contracts, with price riding above a rising 10/20-day
MA. Trigger on the first day the high clears the flag high on ≥1.2× volume;
skip if it gaps > 5 % over the pivot. Entry ≈ pivot + 0.2 %.

**Episodic pivot** — gap up ≥10 % on ≥3× 50-day volume in a stock that was
*not* already extended (≤30 % 3-month gain). Entry ≈ opening-range-high break
(open + 0.2 %).

**Risk / management** — risk 0.5 % of equity per trade at the stop; initial
stop = entry − 1 ADR (proxy for low-of-day); max 25 % of equity per name, 8
positions, no margin. Profit taking: ⅓ comes off at a resting **+2R limit**
(`management.partial_target_r`) or, failing that, after 3 days if the trade is
green; either way the stop lifts to breakeven and the rest trails a close below
the 10-day SMA; 60-day maximum hold. A breakout that has done **nothing after 5
sessions** — closing at or under the entry, never having shown +1R, no partial
taken — is cut at the close (`management.time_stop_days`, 0 = off): dead
money is risk and slot cost. No new longs while QQQ is below its 20-day SMA
or fewer than 40 % of the universe is above its 20-day MA.

**Portfolio gates** (on top of per-trade sizing) — open **heat**, the sum of
what every open position would lose from the latest price to its stop plus
what every resting entry would lose if it filled and stopped, may not exceed
4 % of equity (`risk.max_portfolio_heat_pct`); a **daily loss
circuit-breaker** stops new entries for the rest of the session once equity is
down 3 % from the day's first pass (`risk.daily_loss_limit_pct`) while exits
keep running; and no more than 3 positions in one theme
(`risk.max_positions_per_theme`). A plan refused by a gate is logged as `RISK
… not opened: …`, shown on the desk, and recorded in the shadow ledger so the
review can tell whether the cap cost or saved money. Set any of the three to 0
to switch it off.

**Context layers** (see [Sentiment and theme momentum](#sentiment-and-theme-momentum)
and [Live context: news, social, options flow, events](#live-context-news-social-options-flow-events)) —
theme/segment momentum (hand-kept themes plus one auto-theme per finviz
industry) ranks every signal and gates breakouts; market sentiment (breadth,
optional VIX) extends the regime filter; per-symbol news tone, catalyst tags,
StockTwits/Reddit sentiment, Unusual Whales options flow, earnings proximity,
float and short interest are gathered live for every candidate and folded into
the checklist, the ranking score and the written rationale.

**Adaptive risk** — the trade journal records the realised R of every closed
trade; when the last ten average ≤ −0.2R the risk budget halves, when they
average ≥ +0.6R it grows to 0.625 % (capped at 1 %). "Size up when the
market is paying, size down when it is not", made mechanical.

Every number above lives in [`src/qmag/config.py`](src/qmag/config.py) and can
be overridden from a YAML file (`qmag init-config`).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # extras: alpaca, ibkr, mt5 (Windows), finbert

# Start the dashboard, then open Settings (⚙ in the header) to save your API keys
qmag dashboard                           # http://127.0.0.1:8765 -> http://127.0.0.1:8765/settings

# Real data (Unusual Whales when its key is saved, Yahoo Finance otherwise)
qmag universe build                      # whole market -> universe/market.txt + fundamentals.csv (~15 min first time)
qmag scan                                # scans universe/market.txt by default
qmag chart SMCI                          # -> reports/charts/SMCI_<date>.png
qmag review SMCI --bundle-only           # the edge bundle an LLM reviewer would be asked to judge
qmag flow SMCI                           # unusual options trades (Unusual Whales key)
qmag edge SMCI                           # the weighted Unusual Whales edge score vs the entry threshold
qmag edge SMCI --rules                   # how every feature's sub-score is derived
qmag paper run                           # one full cycle against the local paper ledger (~1-4 min)
qmag dashboard                           # http://127.0.0.1:8765 — plans, justifications, charts, lookup
qmag backtest --start 2021-01-01 --config my.yaml --symbols NVDA,SMCI,PLTR,QQQ
```

**There is no simulated data anywhere in qmag.** Every bar, headline, option
print and account balance the program shows or trades was downloaded from one
of the sources below or read from files you supplied; `--data synthetic` (or
any other generated source) is refused at every entry point. (The automated
test suite generates its own fixture CSVs under `tests/` and feeds them through
the ordinary CSV provider - the product never does.)

Keys unlock data sources (everything else keeps working without them; the
corresponding source is reported as NOT CONFIGURED on `/status` and never
substituted). The easiest place to enter them is the dashboard's
[Settings page](#settings-page-keys-parameters-download--upload); they can also be
exported as environment variables:

| Variable | Unlocks |
| --- | --- |
| `UNUSUAL_WHALES_API_KEY` (paid) | **The primary data source.** With the key set, `--data auto` (the default) takes daily candles from Unusual Whales, the options-flow scan runs for every ranked candidate and the [edge score](#the-unusual-whales-edge-score) gates every entry. Without it, `auto` falls back to Yahoo bars and both Unusual Whales features show as NOT CONFIGURED. `UNUSUAL_WHALES_RPM` (default 100) caps requests per minute; `UNUSUAL_WHALES_BULK_THRESHOLD` (default 300) is explained under [Data sources](#data-sources). |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | Reddit mentions across r/wallstreetbets, r/stocks, r/Shortsqueeze, … (a free "script" app) |
| **AI providers** (settings page → AI providers; `GEMINI_API_KEY` / `OPENAI_API_KEY` still work as fallbacks) | Any number of language-model vendors — Google Gemini and OpenAI are built in; add Anthropic, xAI, Groq, OpenRouter, DeepSeek, Mistral, Together, a local Ollama / vLLM or any OpenAI-compatible endpoint, each with its own key and URL. Every AI seat (reviewer, the three committee seats, post-mortems, insider analysis, advisor) then picks a provider and one of its live-listed models. |
| `QMAG_SCORER=finbert` (with `pip install -e ".[finbert]"`) | FinBERT instead of VADER + finance lexicon for headline tone |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper / live broker |
| `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_DATA_CLIENT_ID` | Interactive Brokers broker and data |
| `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_PATH`, `MT5_SYMBOL_PREFIX`, `MT5_SYMBOL_SUFFIX`, `MT5_MAGIC` | MetaTrader 5 broker and data (Windows terminal) |

finviz fundamentals/news, Yahoo news/calendar and the StockTwits public stream
need no keys.

Tests: `pytest`.

## Handover: run it, operate it, take it over

Everything an operator - or another engineer / coding agent - needs to run
this project from a fresh clone, keep it running, and change it safely. The
sections after this one explain the *trading* logic in depth; this one is
about the *software*.

### 1. What you are looking at

* One Python package, `src/qmag` (installed as `qmag`, CLI entry point
  `qmag = qmag.cli:app`). No database, no message queue, no JavaScript build:
  state is JSON/YAML files in a **state directory** (default `paper_state/`),
  prices are CSV files in a **cache directory** (default `data/cache/`), the
  dashboard is FastAPI + Jinja2 templates with a few inline scripts.
* Three long-running roles, all reading the same state directory:
  `qmag daemon` (the scheduler that trades), `qmag dashboard` (the web UI,
  read-mostly but can run cycles, place manual orders, halt), and the CLI
  (one-off commands). They coordinate only through files: `trader.json`,
  `halt.json`, `settings.yaml`, `settings.env`, `connections.json`,
  `daemon.lock`. Any of them may be restarted independently.
* Every parameter is a field on a frozen dataclass in `config.py`;
  every credential is an environment variable (saved by the settings page
  into `settings.env`, permission 0600, gitignored). There are no other
  configuration mechanisms.

### 2. Prerequisites

* Python **3.11 or newer** (developed and tested on 3.12), `pip`, `git`.
* Outbound internet: Yahoo Finance and finviz work without keys; Unusual
  Whales, Alpaca, Reddit and the LLM providers need keys. Nothing runs without
  network except the test suite and `--data csv`.
* Disk: the whole-market price cache is roughly 1-2 GB of CSV after a year of
  daily bars; charts are ~100 kB each.
* Time zone: everything is scheduled in **America/New_York**; the host clock
  may be anything (the code converts), but set `TZ=America/New_York` for
  readable logs.
* Optional: Docker (see `Dockerfile`, `deploy/start.sh`), systemd
  (`deploy/qmag.service`), Chrome/any browser for the dashboard.

### 3. Install and first run (10 minutes)

```bash
git clone <this repository> qmag && cd qmag
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                    # core + pytest; add ,alpaca / ,ibkr / ,mt5 / ,finbert as needed

# 1. Prove the install: the whole test suite runs on generated CSV fixtures, no network, ~2.5 min
python -m pytest -q

# 2. Start the dashboard and save keys / choose the data source on its settings page
qmag dashboard --port 8765 --state-dir paper_state      # http://127.0.0.1:8765/settings

# 3. Build the scan universe (whole US market, ~15 min; needs finviz/Yahoo reachability)
qmag universe build

# 4. One trading cycle against the built-in paper ledger, then look at the desk
qmag paper run --state-dir paper_state
open http://127.0.0.1:8765

# 5. Let it run
qmag daemon --state-dir paper_state                     # paper ledger; --broker alpaca for an Alpaca paper account
```

Without any key: the data source is Yahoo Finance, the broker is the built-in
paper ledger, news/social come from finviz/StockTwits, and every paid source
shows **NOT CONFIGURED** on `/status` (and is skipped, never faked). That is a
complete, working configuration for evaluating the system.

### 4. Every way to run it

| Mode | Command | Notes |
| --- | --- | --- |
| Dashboard only | `qmag dashboard [--host 0.0.0.0] [--port 8765] [--broker paper] [--state-dir paper_state]` | Serves the desk over an existing state dir; can run paper cycles, manual trades, halt. |
| Daemon | `qmag daemon [--broker ...] [--state-dir ...] [--yes-live]` | The 24/7 scheduler. Refuses to start twice on one state dir (`daemon.lock`). `--show-schedule` prints the next 7 days; `--once TASK` runs one task (`premarket`, `post_open`, `focused`, `movers`, `after_close`, `insider_scan`, `learn`, `universe`) and exits. |
| One cycle | `qmag paper run [--focused]` | Same code the daemon runs, once. |
| Research | `qmag scan`, `qmag chart SYM`, `qmag backtest`, `qmag optimize`, `qmag review SYM`, `qmag flow SYM`, `qmag edge SYM` | Read-only: never touch the broker or trader state. |
| Ops | `qmag status [--probe]`, `qmag broker-test`, `qmag halt`, `qmag resume`, `qmag alert-test`, `qmag paper status`, `qmag accounts [--refresh]` | See [Operating controls](#operating-controls-kill-switch-alerts-api-budget-remote-access) and [Several accounts](#several-accounts-one-desk-per-account). |
| Docker | `docker build -t qmag . && docker run -p 8765:8765 -v qmag_state:/app/paper_state -v qmag_data:/app/data -e QMAG_BROKER=paper qmag` | `deploy/start.sh` runs dashboard + daemon in one container; builds the universe on first start; live brokers need `QMAG_YES_LIVE=1`. Put keys in `-e` vars or mount a `settings.env` into the state volume. |
| systemd | `deploy/qmag.service` | One unit for the daemon; copy it as `qmag-dashboard.service` with `ExecStart=... dashboard` for the UI. Credentials via `EnvironmentFile`. |
| Remote access | `PORT=8855 bash deploy/install-tunnel.sh` (on the host) | Installs `cloudflared` and a `qmag-tunnel` systemd unit exposing the loopback dashboard over HTTPS; refuses to start unless `QMAG_DASHBOARD_PASSWORD` is set. `TUNNEL_TOKEN=…` for a named tunnel with a fixed hostname. See [Remote access](#operating-controls-kill-switch-alerts-api-budget-remote-access). |
| IB Gateway on the same VM | `bash deploy/install-ibgateway.sh` then `--wire` | Docker + `ghcr.io/gnzsnz/ib-gateway` (IB Gateway + IBC + Xvfb, arm64/amd64). Credentials only in `~/ibgateway/.env` (0600). API on `127.0.0.1:4002` (paper). `--wire` installs `ib_async`, saves `IBKR_*`, proves order routing with `qmag broker-test` and switches the units to IBKR paper. Once the gateway answers, `--data auto` also reads daily bars from IB (delayed without a subscription) and keeps Unusual Whales for flow / edge. Walk-through: [IBKR: paper trading and market data, step by step](#ibkr-paper-trading-and-market-data-step-by-step). |
| A second account on the same VM | `bash deploy/add-desk.sh NAME --broker alpaca [--port 8856]` (on the host) | One desk per account: creates `desks/NAME`, its own `qmag-daemon-NAME` / `qmag-dashboard-NAME` units and lists it in the main desk's `QMAG_DESKS`, so the main dashboard's **accounts** page shows every account. Credentials go on the new desk's own settings page. See [Several accounts](#several-accounts-one-desk-per-account). |
| Plain VM over SSH | `deploy/push-vm.sh ubuntu@HOST -i ~/.ssh/key --port 8855` | Ships the committed tree (`git archive`, so no secrets or state), then runs `deploy/install-vm.sh` on the host: venv, `pip install`, two systemd units (`qmag-dashboard` on `127.0.0.1:PORT`, `qmag-daemon`, both enabled at boot), universe build if missing. Re-run after every commit to update; state and `.env` on the host are untouched. Reach the UI with `ssh -L PORT:127.0.0.1:PORT ubuntu@HOST`. Logs: `journalctl -u qmag-dashboard -u qmag-daemon -f`. |

Common options on most commands: `--data auto|yfinance|unusual_whales|ibkr|mt5|csv`,
`--csv-dir`, `--universe FILE`, `--symbols A,B,C`, `--config my.yaml`,
`--broker paper|alpaca|alpaca-live|ibkr|ibkr-live|mt5|mt5-live`,
`--state-dir DIR`, `--options-flow/--no-options-flow`, `-v`.
`--data auto` means Unusual Whales when its key is present, else Yahoo.

### 5. Where everything lives on disk

State directory (`--state-dir`, default `paper_state/`; **back this up** - it is
the trading book):

| File | Written by | What it is |
| --- | --- | --- |
| `trader.json` | trader | The book: managed positions (entry, stop, target, partial done, plan, features, MFE/MAE), pending buy-stops, closed trades (the journal, with post-mortems), arming list, shadow ledger, day-open equity. |
| `ledger.json` | paper broker | Built-in paper account: cash, positions, orders, last prices. Only with `--broker paper`. |
| `last_report.json` / `last_full_report.json` | session | The latest cycle (plans, rejected, actions, gaps, regime, heat, day P&L) and the latest whole-universe scan. The desk renders these. |
| `screens.json` | session | Pre-market / movers screener hits for the day. |
| `settings.yaml` / `settings.env` | settings page | Saved strategy config (takes over from `--config` once it exists) and saved credentials (0600). `settings.env` is exported into `os.environ` on load. |
| `llm_providers.json` | settings page (AI providers) | The AI providers: id, label, kind (`gemini` / `openai`-compatible), endpoint URL and key per vendor (0600, gitignored). Built-in `gemini` / `openai` rows fall back to the environment keys when the file has none. |
| `learning_overrides.yaml` / `learning_report.json` | learning | Bounded knob adjustments in force (with evidence) and the last review. |
| `connections.json` | health registry | Last outcome per connection (used by `/status`, `qmag status`, the header pill). |
| `daemon_status.json` / `daemon.lock` | daemon | Heartbeat, next task, per-task run counts and errors, pid; the single-instance lock. |
| `halt.json` | kill switch | Present only while trading is halted (`reason`, `by`, `at`, `flattened`). |
| `account.json` | session | This desk's account snapshot after every cycle: broker-reported equity and cash (with currency), every holding the broker reports marked at the latest real bar (unrealised P&L, stop / target when qmag manages it), resting orders, realised P&L from the journal, day-open equity. Read by the accounts page of this and every other desk. |
| `order_tests.json` | broker tests | Last 20 test orders with every step. |
| `insider_scan.json` | Saturday scan | Flagged tickers, their share-price backdrop, the AI catalyst analysis, the weeks' history and the flag-outcomes scorecard. |
| `tunnel_url.txt` | `deploy/tunnel-watch.sh` | The quick tunnel's current public address (shown on the settings page). Written on the host that runs the tunnel. |
| `advisor.json` | advisor page | The advisor conversation and its pending (not yet accepted) setting proposals. Safe to delete. |
| `context_cache.json`, `uw_cache.json` | context layer | Short-TTL caches of news/social/flow reads and raw Unusual Whales responses. Safe to delete. |
| `charts/` | charts | One annotated PNG per plan, served at `/charts/…`. |

Cache directory (`data/cache/`, safe to delete - rebuilt on demand): one CSV
of daily bars per symbol (incremental; the provider fetches only what is
missing), `uw_budget.json` (today's Unusual Whales call count / pause; move it
with `UNUSUAL_WHALES_BUDGET_FILE`).

Repository files you may edit: `universe/themes.yaml` (hand-kept theme →
tickers), `universe/default.txt` (starter list). Generated and committed for
convenience: `universe/market.txt`, `universe/fundamentals.csv` (rebuild with
`qmag universe build`).

### 6. Environment variables

All of these can be entered on the settings page (saved to `settings.env`)
or exported before starting a process. Secrets are masked in every UI and API.

| Variable | Purpose |
| --- | --- |
| `QMAG_DATA` | Default data source when `--data auto`: `yfinance`, `unusual_whales`, `ibkr`, `mt5`, `csv`. Left on `auto`, qmag picks IBKR when the gateway answers, else Unusual Whales when its key is set, else Yahoo. |
| `UNUSUAL_WHALES_API_KEY` | Unusual Whales (price data, options flow, edge score, screeners, insider scan). |
| `UNUSUAL_WHALES_RPM` | Process-wide request throttle (default 100/min). |
| `UNUSUAL_WHALES_DAILY_CAP` | Stop calling after this many requests per UTC day (0 = uncapped). |
| `UNUSUAL_WHALES_BULK_THRESHOLD` | Sweeps over this many symbols use Yahoo batches instead (default 300; 0 = always UW). |
| `UNUSUAL_WHALES_BUDGET_FILE` | Location of the shared daily budget file (tests / multi-host). |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper *or* live keys - use the pair that matches `--broker`. |
| `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `IBKR_DATA_CLIENT_ID` | TWS / IB Gateway (7497 paper, 7496 live; Gateway 4002 / 4001). |
| `IBKR_PREFER_DATA` | `yes` (default): `--data auto` reads bars from IB whenever the gateway answers. `no`: never pick IB automatically. |
| `IBKR_DATA_FALLBACK` | `yes` (default): symbols IB cannot serve are loaded from Yahoo and counted in the report. `no`: leave them out. |
| `IBKR_VOLUME_MULTIPLIER` | `auto` (default): measure IB's volume scale (lots of 100 vs shares) against Yahoo's SPY once a week. Or force `1` / `100`. |
| `IBKR_DATA_CONCURRENCY`, `IBKR_MARKET_DATA_TYPE` | Historical requests in flight (default 8) and the market data type asked for before requesting bars (default 3 = delayed, works without subscriptions). |
| `MT5_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_SYMBOL_PREFIX`, `MT5_SYMBOL_SUFFIX`, `MT5_MAGIC` | MetaTrader 5 (Windows only). |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | Reddit social read (optional; StockTwits works without). |
| `GEMINI_API_KEY` (`GOOGLE_API_KEY`), `OPENAI_API_KEY` (`QMAG_LLM_API_KEY`, `QMAG_LLM_BASE_URL`) | Environment fallbacks for the two built-in AI providers. Every other vendor — and normally these two as well — is entered under **AI providers** on the settings page and kept in `llm_providers.json`. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `QMAG_ALERT_WEBHOOK_URL` | Push alerts. |
| `QMAG_DASHBOARD_PASSWORD` | Turns on the dashboard sign-in page (cookie sessions + bearer auth for `/api/*`). Required before exposing the port through a tunnel or a public bind address. |
| `QMAG_DESK_NAME` | This desk's label on the accounts page (default: the state directory's name). |
| `QMAG_DESKS` | Other desks to show on the accounts page: `name=/path/to/state_dir[\|http://its-dashboard]`, comma-separated. Read-only: their `account.json`, daemon heartbeat and `halt.json` are shown as they are. |
| `QMAG_SCORER` | Headline sentiment scorer: `vader` (default) or `finbert` (needs the extra). |
| `QMAG_BROKER`, `QMAG_STATE_DIR`, `QMAG_DASHBOARD_PORT`, `QMAG_ARGS`, `QMAG_YES_LIVE` | Docker entrypoint (`deploy/start.sh`) only. |

### 7. A day in the life of the daemon (what to expect in the logs)

All times New York, weekdays that are not NYSE holidays (`market_calendar.py`):

1. **08:30, 09:20 - `premarket`**: one screener request for gappers; hits join the arming list as EP candidates.
2. **09:35 → close − 5 min, every 5 min - `focused`**: refresh only the arming list, open positions, pending entries and today's screener hits. Manage exits (stops, partials, trails, failed-breakout, time stop). Buy `confirmed` breakouts at market when price holds above the pivot on paced volume. Log lines: `HOLD`, `PACE`, `BUY`, `EXIT`, `PARTIAL`, `RISK`, `DATA`, `HALT`.
3. **09:40 - `post_open`**: whole universe on the first bars, so an EP no screener surfaced is still caught.
4. **10:00 → close − 30 min, every 30 min - `movers`**: top gainers on relative volume get the full check.
5. **close + 20 min - `after_close`** (the nightly arming scan): whole universe on completed bars; size, justify and chart tomorrow's plans; park brackets if `entry.mode` is `resting`; rebuild the arming list; write `last_full_report.json`; score shadow trades.
6. **Saturday 10:00 - `insider_scan`**, **Saturday 11:00 - `learn`**, **Sunday 12:00 - `universe`**.

Between tasks the loop sleeps in 1-second slices (so `SIGINT`/`SIGTERM` stops it within a second), writes `daemon_status.json` every heartbeat, and re-reads `settings.yaml` at every tick so a schedule saved on the settings page applies without a restart. One failing task is logged, alerted, recorded on `/status` and never takes the process down.

### 8. How a cycle works (the code path to read first)

`TradingSession.cycle()` (`session.py`) → `load_frames()` (provider + cache, freshness check) → `run_cycle()` (`trader.py`), whose numbered sections are:

0. paper broker fills resting orders against the latest bars (paper only)
1. **reconcile** the book with the broker (fills, broker-side closes, adopted positions)
2. **manage** open positions: stops, `partial_after_days` / `+2R` partial, breakeven, trail MA, `max_hold_days`, failed-breakout exit, **time stop**
3. **regime**: benchmark trend, breadth, VIX → `regime_ok` (unknown = risk-off)
4. **new ideas**: detectors (`setups/`) → `plan.py` (sizing, checklist, context gates, adaptive risk) → `rationale.py` → context (`context/gather.py`, edge score, LLM reviewer) → **portfolio gates** (heat, daily loss, theme) → confirm and place (`broker.py`)
5. **arming list** for tomorrow's focused passes
6. **learning**: score shadow trades on completed bars

Then the session writes `last_report.json` + charts, records health, and hands
the report to `alerts.py`. `focused_cycle()` is the same with a narrowed
symbol scope and a `LiveClock` (partial bar, volume pace). The kill switch
(`halted=`) short-circuits step 4 and cancels resting entries.

### 9. Invariants - do not break these

* **Nothing is fabricated.** A source that is off, unconfigured or failing yields *no value* plus a recorded gap; checks that need the value fail. No default scores, no synthetic bars, no fallback numbers. Tests generate CSV fixtures only under `tests/`.
* **Fail closed on money.** Unknown regime = risk-off; missing required connection = no new entries; unreachable LLM with `fail_closed` = skip; live accounts need explicit confirmation (`--yes-live`, `confirm_live`); a stop is never placed at or above the market.
* **Open positions are always protected.** Every managed position has a stop (or OCO) resting at the broker; the kill switch, the daily loss limit and data gaps stop *entries*, never exit management.
* **Config is frozen dataclasses**; change values with `with_overrides({"section.key": v})`, never by mutation. Every new field needs a default, a `FIELD_HELP` entry (tested), and - if it has a legal range - a line in `validate_config`.
* **Secrets never leave the process** except to their own provider: masked in the settings UI/API/export, absent from alert payloads and logs, `settings.env` is 0600 and gitignored.
* **State files are the API between processes.** Read-modify-write them through their classes (`TraderState.load/save`, `ConnectionRegistry`, `SettingsStore`, `set_halt`), which handle merging and atomic writes.
* **Times are New York.** Use `market_calendar.NY`, `trading_days()`, `expected_last_session()`; never `datetime.now()` without a zone.

### 10. Making changes

| You want to… | Touch | Then |
| --- | --- | --- |
| Add / change a strategy parameter | the dataclass in `config.py`; `FIELD_HELP` (+ `CHOICES`, `QUICK_FIELDS`, `validate_config` as needed) in `settings.py` | `test_every_strategy_setting_has_help_text` enforces the help; README rules section if it changes behaviour. |
| Add an exit or entry rule | `trader.py` step 2 or 4 **and** the mirror in `backtest.py` so research matches live | Add a `learning.py` post-mortem note if the exit reason is new; a unit test like `test_time_stop_cuts_dead_breakout`. |
| Add a context source | `context/sources.py` (+ `gather.py`), a `ConnectionSpec` + `_configured` branch + probe step in `health.py` | It must report `unavailable` cleanly; the plan's data gaps pick it up automatically. |
| Add an Unusual Whales edge feature | `context/edge.py` `FEATURES` + `DEFAULT_EDGE_WEIGHTS` in `config.py` | `qmag edge SYM --rules` should explain it; health specs are generated per feature. |
| Add a broker | implement the `Broker` protocol in `broker.py`, register in `make_broker`, add to `LIVE_BROKERS` if live | Run `qmag broker-test --kind bracket` against it; the trader only uses the protocol methods. |
| Add a daemon task | `Task(...)` in `Daemon.__init__` with a `times_on` callable reading `cfg.schedule` | Also list it in `--once` help and `/status`. |
| Add a dashboard page or API | route in `dashboard.py` (`create_app`), template extending `base.html`, macros in `_macros.html` | Use `{{ m.info("…") }}` for hover help; test with `TestClient`. |
| Add an alert | call `session.alerts.send(title, body, level, key)` where the event happens | Use `key` for conditions that persist so they are not repeated every pass. |
| Add a CLI command | `@app.command()` in `cli.py`, build a `TradingSession` from `SessionSettings` | Add a row to the table at the top of this README. |

Conventions: standard library + pandas idioms, type hints, docstrings that say
*why*; no formatter is enforced but keep to the existing style (long lines are
fine, one statement per line). One commit per logical change with a
descriptive message. Tests live in `tests/`, use `pytest`, and must not touch
the network: build sessions with `tests/synthetic.write_csv_universe` +
`SessionSettings(**u.session_kwargs(), overrides=u.overrides(...))`, or hand
frames straight to `run_cycle` with a `PaperBroker`.

### 11. Tests

```bash
python -m pytest -q                       # everything, ~2.5 min, no network
python -m pytest tests/test_trader.py -q  # one area
```

| File | Covers |
| --- | --- |
| `test_setups.py`, `test_backtest.py` | detectors on hand-built frames; the portfolio simulator and metrics |
| `test_trader.py`, `test_plan_and_orders.py`, `test_tiered.py` | cycle replay on generated data, order/exit mechanics, arming + focused passes + volume pace |
| `test_context.py`, `test_context_layer.py`, `test_options_flow.py`, `test_edge.py`, `test_reviewer.py`, `test_fundamentals.py` | context sources (mocked HTTP), scoring, flow, edge features, LLM reviewer parsing |
| `test_integrity.py`, `test_providers_brokers.py`, `test_ops.py`, `test_settings.py` | no-fabrication guarantees, providers and brokers with fakes, health/status/daemon, settings store and masking |
| `test_learning.py`, `test_desk_actions.py`, `test_risk_ops.py` | journal + review + shadows, manual desk actions + order tests + daemon lock, portfolio gates + kill switch + alerts + UW budget + time stop + hover help |

### 12. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `qmag daemon` exits with "already running" | Another daemon holds `daemon.lock` for that state dir (pid shown). Stop it or use another `--state-dir`. A stale lock from a dead pid is taken over automatically. |
| Header shows **DATA GAPS**, no new entries | A required source (price data, universe, regime inputs, broker) is down or stale. `/status` lists what and since when; `qmag status --probe` retests. Entries resume by themselves once it answers. |
| `no bars for SYM` on the lookup page | `load_frames` fetches only `warmup_bars × 3` days back from today; a symbol with no recent bars (delisted, wrong suffix) cannot be analysed. |
| Yahoo returns empty / rate-limited | Wait or switch `QMAG_DATA` to `unusual_whales`; the cache keeps what was already fetched. |
| Unusual Whales lines read PAUSED / CAP REACHED | Daily budget (see [Operating controls](#operating-controls-kill-switch-alerts-api-budget-remote-access)). Resets at 00:00 UTC. |
| Everything shows **TRADING HALTED** | The kill switch is on (`halt.json`). `qmag resume` or the button on `/status`. |
| Dashboard port in use | `qmag dashboard --port 8766`. |
| Alpaca "forbidden" / IBKR connection refused | Paper vs live key pair mismatch; TWS/Gateway not running or API not enabled / wrong port. `qmag broker-test` shows the failing step. |
| Positions at the broker the desk does not manage | Bought outside qmag: they are listed as *unmanaged* and left alone by design. |
| Charts missing | `charts: False` on the session (CLI `--no-charts`), or matplotlib backend issues in a headless container (`MPLBACKEND=Agg` is set by the code; check the log). |


## From signal to order: the pipeline

1. **Universe** — `qmag universe build` downloads the official NASDAQ Trader
   listings (`nasdaqlisted.txt`, `otherlisted.txt`), drops ETFs, warrants,
   units, preferreds, SPAC rights and test issues, pulls a year of history for
   the rest in batches of 250 and keeps names with price ≥ $3, 20-day dollar
   volume ≥ $5 M and 60+ bars. The result is written to `universe/market.txt`
   (sorted by liquidity) and becomes the default for every command; the daemon
   rebuilds it every Sunday. Prices are cached per symbol and refreshed
   *incrementally*, so a full 2,900-name cycle takes about a minute.
2. **Setup detection** — the breakout and episodic-pivot detectors run on
   every name; flags within 10 % of their pivot go on the watchlist.
3. **Context** — for the ranked candidates (top `context.max_symbols_per_cycle`,
   cached 30 min) the gatherer pulls, in parallel: finviz fundamentals and
   news, Yahoo news and earnings calendar, StockTwits (and Reddit / Unusual
   Whales when keys are set). Headlines are scored and tagged with catalysts
   (earnings, guidance, FDA, offering, M&A, contract, analyst, legal, insider,
   squeeze, index inclusion…); social posts are scored with platform labels
   plus a trader-slang lexicon; options flow becomes a −1…+1 tilt.
4. **Checklist** — each idea becomes a `TradePlan`
   ([`src/qmag/plan.py`](src/qmag/plan.py)) with an explicit pass/fail for:
   setup triggered, momentum leader, theme strength, sentiment (CSV), market
   regime, liquidity, price floor, stop within 2 ADR, stop ≤ 15 % of price,
   earnings window (breakouts within 3 days of a print are skipped), news
   flow (very negative tone skips), social crowding (near-unanimous
   bullishness skips episodic pivots), options flow, **Unusual Whales edge
   score ≥ threshold with enough coverage** (fails closed — see
   [The Unusual Whales edge score](#the-unusual-whales-edge-score)), positive
   size. Failed plans are reported, justified and charted but not traded.
5. **Sizing** — shares = risk budget ÷ (entry − stop), capped by the 25 %
   position limit, gross exposure and cash. The budget is 0.5 % of equity
   scaled by the adaptive-risk multiplier from the trade journal.
6. **Justification** — every plan carries a written rationale
   ([`src/qmag/rationale.py`](src/qmag/rationale.py)): the setup in numbers,
   why the entry is where it is, the sizing arithmetic, why the stop sits
   there, the profit-taking ladder, the market/sector/sentiment read, a bull
   case, a bear case and a verdict. Optionally an **analyst committee**
   ([`src/qmag/llm.py`](src/qmag/llm.py)) — three separate model calls, a
   bull seat, a bear seat and a risk chair, each with its own provider and
   model — argues the plan; by default its ruling is advisory
   (`committee.can_veto: false`).
   Last, the additive **LLM reviewer** ([`src/qmag/reviewer.py`](src/qmag/reviewer.py))
   can be handed the whole edge bundle — setup, sizing, targets, checklist,
   rationale, news/events, social, options flow, fundamentals, committee
   debate, regime, portfolio — and return a strict-JSON BUY/SELL/HOLD verdict
   with confidence, thesis, catalysts, risks, invalidation and a size note that
   the trader records, gates on, or sizes by (`reviewer.mode`).
7. **Orders** — a triggered setup is bought at market with an OCO
   (⅓ at the +2R target / stop) plus a protective stop on the rest; a
   watchlist flag gets a buy-stop-**limit** bracket at the pivot (limit at
   pivot + 5 % so gaps are never chased). Every cycle re-checks that the
   resting sell orders match the plan and repairs them if not.
8. **Chart** — `paper_state/charts/SYMBOL_DATE.png` for every plan and open
   position: candles, volume, 10/20/50 SMAs, the shaded flag, pivot (amber),
   entry (green), stop (red), target (blue, dashed) with R distances.
9. **Journal** — when the last share of a position is sold the trade is
   written to `trader.json` with exit reason, P&L and realised R; the last
   ten R-multiples drive the next cycle's risk budget.

### Data sources

Note: the browser dashboard and the trading loop run on **daily bars refreshed
every cycle** (intraday cycles see today's partial bar); "real time" here
means every 30 minutes during the session, not tick by tick.

* `--data auto` (default) — the source saved on the Settings page when there
  is one (`QMAG_DATA`), otherwise `unusual_whales` when `UNUSUAL_WHALES_API_KEY`
  is set, otherwise `yfinance`. The resolved source is what every report,
  status line and `/status` page names.
* `--data unusual_whales` — daily candles from `GET /stock/{ticker}/ohlc/1d`
  (**as traded**, not split-adjusted), one request per symbol under the
  shared throttle, cached under `data/cache/uw/`. Unusual Whales has no bulk
  bars endpoint, so a load of more than `UNUSUAL_WHALES_BULK_THRESHOLD`
  symbols (default 300 — i.e. the whole-market sweep) is served by the Yahoo
  batch downloader instead and the price-data status says so
  (`… sweep served by yfinance`). Candidates, positions, lookups and any
  explicit `--symbols` list stay on Unusual Whales. Set the threshold to `0`
  to insist on Unusual Whales for everything (a 2,900-name sweep is then
  25-30 minutes at 100 requests/minute). The two caches are kept apart
  because adjusted and as-traded bars must never be merged.
* `--data yfinance` — free daily bars (split-adjusted), cached under `data/cache/`.
* `--data ibkr` — daily TRADES bars from TWS / IB Gateway via `ib_async`
  (`pip install -e ".[ibkr]"`, own client id `IBKR_DATA_CLIENT_ID`, default
  18). **`--data auto` picks this by itself whenever the gateway is
  answering** on `IBKR_HOST:IBKR_PORT` (unless `IBKR_PREFER_DATA=no`), so a
  paper account doubles as the price feed and the Unusual Whales budget is
  kept for the flow and edge reads nothing else provides. Details and the
  full setup walk-through: [IBKR: paper trading and market data, step by
  step](#ibkr-paper-trading-and-market-data-step-by-step). In short:
  delayed end-of-day bars work without any paid subscription (market data
  type 3 is requested), requests run 8 at a time so a whole-market cold load
  is minutes, IB's volume scale (lots of 100 vs shares) is measured once
  against Yahoo's SPY and remembered, symbols IB has no bars for are filled
  from Yahoo and counted in the report's data stats, and IB bars keep their
  own cache under `data/cache/ibkr/` (split- but not dividend-adjusted, so
  never merged with Yahoo's).
* `--data mt5` — daily bars from a MetaTrader 5 terminal (Windows;
  `pip install -e ".[mt5]"`). Brokers list US stocks under their own names,
  so set `MT5_SYMBOL_PREFIX` / `MT5_SYMBOL_SUFFIX` (e.g. `.US`) — frames are
  keyed by the plain ticker so the rest of the pipeline is unchanged.
* `--data csv --csv-dir data/csv` — one `SYMBOL.csv` per ticker with
  `date,open,high,low,close,volume` columns (any vendor export works).

There is no generated / synthetic source: `--data synthetic` exits with
*qmag never uses simulated or generated prices*.

Yahoo and MT5 share the same incremental per-symbol cache (`data/cache/`,
split- and dividend-adjusted), so you can scan on free Yahoo data and execute
on MT5, or switch between them without re-downloading history. Sources whose
bars are adjusted differently keep their own directory so histories are never
merged: Unusual Whales candles (as traded) in `data/cache/uw/`, IBKR bars
(split- but not dividend-adjusted) in `data/cache/ibkr/`. Universe building
always uses Yahoo batches.

### Universe

`universe/market.txt` (built by `qmag universe build`, refreshed weekly by the
daemon) is the whole liquid US market and is used automatically when present.
`universe/default.txt` is a small starter list for quick experiments. Override
with `--universe path/to/list.txt` or `--symbols A,B,C`. Backtests over
`market.txt` still carry survivorship bias (today's listings only).

## Sentiment and theme momentum

Three different things get called "sentiment"; they are handled differently
because they automate differently.

### Theme / segment momentum (on by default)

Kullamägi's phrasing is "the strongest stocks in the strongest themes".
`universe/themes.yaml` groups tickers into themes (AI semis, nuclear, space,
GLP-1, …). For every theme an index is built from the **median** member's
daily return using the prices you already loaded, ranked against the other
themes on `1m + 0.5 × 3m` return, and its internal breadth (share of members
above their 20-day MA) is measured. The median is deliberate: a theme is a
group moving *together*, and with an equal-weight mean one stock tripling
made a six-name industry read "+100 %" while the other five went nowhere.
Groups with fewer than `themes.min_theme_members` (3) scanned members do not
rank at all - their stocks count as themeless. Each stock inherits the best
percentile of the themes it belongs to. All of it is a time series, so
backtests only ever see what was knowable that day.

* **Ranking** — `themes.score_weight × theme_pct` is added to every signal's
  score, so when there are more setups than slots the one in the hotter group
  gets the slot. This is the main mechanism.
* **Gating** — breakouts in themes ranked in the bottom 30 % or with less
  than 40 % breadth are skipped (`themes.min_theme_percentile`,
  `themes.min_theme_breadth`). The gate applies to **breakouts only**
  (`themes.apply_to`). Episodic pivots deliberately bypass it: EPs come from
  *neglected* names and groups, and on real data the gate removed 25 EPs with
  +1.3R average expectancy — including ASTS in May 2024 (+27R) out of a
  "space" theme that was dead at the time.
* **Industry auto-themes** — `qmag universe build` (and the daemon's Sunday
  rebuild) also crawls finviz for every universe name and caches sector,
  industry, market cap, float, short interest and next earnings in
  `universe/fundamentals.csv` (`qmag universe fundamentals` refreshes it
  alone). With `themes.use_industries` (default on) every finviz industry
  with ≥ 4 liquid members becomes a theme (`Industry: Oil & Gas Refining &
  Marketing`, `Industry: Semiconductors`, … about 130 of them), so the whole
  market is covered without anyone maintaining a YAML; hand-kept themes still
  apply on top and win ties.
* `qmag scan` prints the theme leaderboard. `--no-themes` turns the layer off;
  inline `themes.groups:` in your YAML overrides the file.

Whether the hard gate helps is an empirical question that a 36-name sample
cannot answer, so `themes.min_theme_percentile` (0 = gate off) is in the
default walk-forward grid. Let out-of-sample results decide.

### Market sentiment (regime gates)

`RegimeFilter` now has three independent gates; all active ones must pass
before a new long is taken:

| Gate | Setting | Default |
| --- | --- | --- |
| Benchmark trend | `regime.benchmark` above `regime.ma_length`-day MA | QQQ > 20d, on |
| Breadth | share of the scan universe above its `breadth_ma_length` MA ≥ `min_breadth` | 40 %, on |
| Volatility | `^VIX` close < `max_vix` | off (`null`); set e.g. `30.0` |

Breadth is computed from the universe itself, so it directly measures whether
momentum names are working rather than whether the index is up. Setting
`max_vix` makes the loaders fetch `^VIX` automatically. `regime.min_breadth`
is in the default optimiser grid too.

### Historical sentiment for backtests (CSV)

Live sentiment cannot be backtested honestly without point-in-time archives,
so the backtester takes a CSV of `date,symbol,score[,buzz]` rows from any
vendor:

```bash
qmag backtest --start 2022-01-01 --sentiment-csv data/sentiment.csv
```

* `score` in −1..+1. `sentiment.min_score` skips bearish tone;
  `sentiment.max_score` is a **crowded-trade guard** — EPs in particular work
  best while the stock is still neglected, so capping euphoric readings is the
  more useful direction.
* `sentiment.score_weight × score` is added to the ranking score.
* Readings forward-fill for `max_staleness_days`, then become NaN, which
  passes the filter (missing data is not a signal).
* New sources implement `SentimentProvider.scores()` in
  [`src/qmag/sentiment.py`](src/qmag/sentiment.py) — one method.

## Live context: news, social, options flow, events

In live/paper trading every ranked candidate gets a `ContextReport`
([`src/qmag/context/`](src/qmag/context)) before it is sized. Sources, all
optional and all cached for `context.cache_minutes`:

| Source | What is used | Needs |
| --- | --- | --- |
| finviz ([finvizfinance](https://github.com/lit26/finvizfinance)) | Headlines for the last `news_lookback_days`; sector, industry, market cap, float, short float, insider transactions, institutional ownership, analyst recommendation, target price, next earnings date | nothing |
| Yahoo Finance | Headlines and earnings calendar as a second opinion / fallback | nothing |
| StockTwits public stream | Last 30 posts with the poster's Bullish/Bearish label | nothing |
| Reddit | Posts mentioning the ticker across the trading subreddits, scored by text | free script app credentials |
| Unusual Whales options flow (see [Unusual options flow](#unusual-options-flow-unusual-whales)) | Day's bullish vs bearish premium and call/put volume vs 30-day average; the ticker's *unusual* flow alerts (opening OTM trades, sweeps, repeated hits); option-volume percentile vs its own history | `UNUSUAL_WHALES_API_KEY` (on by default; `options_flow.enabled: false` or `--no-options-flow` switches it off) |
| Unusual Whales edge score (see [The Unusual Whales edge score](#the-unusual-whales-edge-score)) | 22 further reads — dealer greeks, GEX walls, net premium, OI build, skew, max pain, IV vs RV, dark pool, short interest, insiders, 13F, congress, analysts, seasonality, earnings reaction, headlines, market and sector tide — weight-averaged into one score with a coverage figure and an entry threshold | `UNUSUAL_WHALES_API_KEY` (on by default; `edge.enabled: false` switches it off) |

**Scoring.** Headlines get `0.5 × VADER + 0.5 × finance-lexicon` with catalyst
tags; `QMAG_SCORER=finbert` swaps in [FinBERT](https://github.com/ProsusAI/finBERT).
Recent headlines weigh more (2-day half-life). Social posts use the platform
label when present (±0.8) and a trader-slang lexicon otherwise; fewer than
`social_min_messages` posts means "no reading", not "neutral". The composite
(`news 1.0, social 0.6, flow options_flow.weight`, weighted by availability)
is added to the ranking score with `context.score_weight`.

**Gates.** `avoid_earnings_within_days` (breakouts only — an EP *is* the
earnings reaction), `news_min_score`, `social_max_score` (crowded EPs),
`options_flow.min_score`. Missing data never fails a gate — with one
deliberate exception, `options_flow.require_bullish`, which demands the
evidence.

### Unusual options flow (Unusual Whales)

Big, aggressive, opening options bets are one of the few places where
informed money shows its hand before the stock moves, so the trader scans
each candidate's options tape and folds what it finds into the edge. The data
comes from the [Unusual Whales API](https://api.unusualwhales.com/docs),
a paid subscription the desk now runs on, so the scan is **on by default**
and switchable: `options_flow.enabled: false` in the config or
`--no-options-flow` on `paper run`, `daemon`, `dashboard` or `review` turns it
off (`--options-flow` forces it on). Without a key nothing is called and the
source reads NOT CONFIGURED. The header pill shows
`options flow · off | on | no key`, and each plan's rationale states which it
was.

For every candidate that reaches the context stage (top
`options_flow.max_symbols_per_cycle` by rank, so the paid call count per
cycle is bounded) three endpoints are read:

| Endpoint | What it gives the edge |
| --- | --- |
| `/stock/{t}/options-volume` | Bullish premium (ask-side calls + bid-side puts) vs bearish premium, call and put volume against their 30-day averages |
| `/option-trades/flow-alerts?ticker_symbol={t}&unusual=true` | The tape's *unusual* trades using Unusual Whales' own preset (volume > OI, all-opening, OTM, single-leg, ask-side ≥ 50 %, ≥ $10k), further filtered to `min_premium` (default $50k), the last `lookback_days` (3) and `max_dte` (90). Each is classified: call bought at the ask or put sold at the bid = bullish; put bought at the ask or call sold at the bid = bearish; sweeps weigh `sweep_weight` (1.5×) |
| `/stock/{t}/unusualness` | Today's option volume as a percentile of the ticker's own ~90-day history |

These become one −1…+1 **flow tilt** (premium tilt + unusual-trade tilt +
volume-ratio nudge, amplified by a high percentile), an **unusual-activity
flag** (any of: ≥ `min_alerts` qualifying trades, call volume ≥
`volume_ratio_unusual` × its average, percentile ≥ `percentile_unusual`) and a
list of the `top_trades` largest trades with strike, expiry, DTE, premium, side,
sweep/floor flags, volume/OI and % OTM. They are used everywhere the rest of
the edge is:

- **Ranking** — the tilt enters the composite context score with `options_flow.weight`.
- **Checklist** — `options_flow` (`min_score`, e.g. `-0.3` vetoes names under heavy put buying) and, if `require_bullish: true`, `unusual_flow_bullish` (tilt ≥ `bullish_threshold` with ≥ `min_alerts` bullish trades; absent data fails, because "require" means the edge must be seen).
- **Rationale** — "Options flow leans bullish (+0.78; call $1.4M vs put $0.4M premium, call volume 3.2× its 30-day average, option volume in the 96th percentile…). Largest unusual trades: $620k of 2026-10-16 60C bought at the ask (sweep, vol/OI 4.6, 13 % OTM); …" and a bull-case line "unusual bullish options activity (2 trades, 1 sweep)" or a bear-case line when protection is being bought.
- **Dashboard** — the options-flow gauge on each plan card is tagged `unusual`, and the plan / lookup pages carry a full panel: tilt, bullish vs bearish premium, call volume vs 30d, percentile, trade counts and a table of the largest unusual trades.
- **LLM reviewer** — the edge bundle carries the same numbers plus the eight largest trades.

```yaml
options_flow:
  enabled: true            # default; false switches the paid scan (and the edge score) off
  min_premium: 50000       # a trade must be at least this big to count
  lookback_days: 3
  max_dte: 90
  weight: 1.0              # share of the composite context score
  min_score: -0.3          # veto names with heavy put buying (null = record only)
  require_bullish: false   # true = only trade when bullish unusual flow is present
  max_symbols_per_cycle: 25
```

Check the feed on one ticker before switching it on for trading:

```bash
qmag flow NVDA                      # tilt, premiums, percentile, largest unusual trades
qmag flow NVDA --min-premium 250000 # only the whales
qmag flow NVDA --json
```

### The Unusual Whales edge score

The flow scan answers one question — is somebody betting big on this name
right now? The Unusual Whales API answers many more, so the trader reads
everything on it that bears on a long entry, scores each read independently
and blends them into **one number that must clear a threshold before a
position is opened** ([`src/qmag/context/edge.py`](src/qmag/context/edge.py)).

**How it is built.** Every feature reads real endpoints and turns them into a
sub-score in −1…+1 (bullish positive) by a stated rule. The edge score is
`Σ weight × sub-score / Σ weight` over the features that **answered**.
`coverage` is the answered weight divided by the applicable weight: it says
how much of the intended evidence was actually seen. The entry gate demands
both `score ≥ edge.threshold` (default `+0.15`) **and**
`coverage ≥ edge.min_coverage` (default 50 %); if the score could not be
computed at all the gate fails. Nothing missing is ever counted as neutral:
it lowers coverage and is listed by name on the plan, in the rationale, in
the LLM bundle and on `/status`. Features that do not apply — the options
reads on a ticker with no listed options (`/stock/{t}/info` → `has_options`)
— drop out of both the score and the coverage.

| Feature (weight) | Reads | Sub-score rule |
| --- | --- | --- |
| **Options positioning & flow** | | |
| Unusual options flow (1.5) | `/stock/{t}/options-volume`, `/option-trades/flow-alerts`, `/stock/{t}/unusualness` | the options-flow tilt above: bullish vs bearish premium, aggressive unusual trades (sweeps weighted), amplified by the option-volume percentile |
| Net premium today (1.0) | `/stock/{t}/net-prem-ticks` | (net call premium − net put premium) / (|net call| + |net put|) summed over today's minute ticks; ask-side buying counts positive, bid-side selling negative |
| Open interest built (0.75) | `/stock/{t}/oi-change` | (call OI change − put OI change) / (|call| + |put|) across the contracts with the largest overnight OI change |
| Dealer delta exposure (0.5) | `/stock/{t}/greek-exposure` | (call delta + put delta) / (|call delta| + |put delta|) of the latest market-maker exposure snapshot |
| Gamma regime & walls (0.5) | `/stock/{t}/greek-exposure`, `/stock/{t}/gex-levels` | +0.5 when net dealer gamma is negative (hedging amplifies moves), −0.25 when positive (pinning); plus room to the call wall: +0.5 at ≥ 8 % above spot, −0.5 within 2 % |
| Options positioning sentiment (0.75) | `/stock/{t}/volatility/option-sentiment` | Unusual Whales' blended VWKS + AVAR positioning score, normalised to −1…+1 |
| Nasdaq Options Pulse (0.5) | `/stock/{t}/options-pulse` | the running daily sentiment score of opening-buy transactions, normalised |
| 25-delta skew (0.5) | `/stock/{t}/volatility/term-structure`, `/stock/{t}/historical-risk-reversal-skew` | today's 25Δ risk reversal (put IV − call IV) for the ~30-day expiry vs its own one-month history; calls bid richer than usual scores positive (z-score / 2, clipped) |
| Max pain (0.25) | `/stock/{t}/max-pain` | spot above the nearest expiry's max pain scores negative (pin / pullback risk into expiry), below positive; scaled 1 / 0.5 / 0.25 for ≤ 3 / 7 / more days to expiry |
| Implied vs realised vol (0.25) | `/stock/{t}/volatility/stats`, `/stock/{t}/volatility/term-structure` | IV close to RV scores mildly positive (moves not yet paid for), IV 60 %+ richer than RV negative (an event is priced); an inverted term structure subtracts 0.3 |
| **Stock tape** | | |
| Dark pool prints (0.75) | `/darkpool/{t}` | notional-weighted position of large off-exchange prints (≥ `dark_pool_min_premium`, $100k) inside the NBBO: at/above the offer +1, at/below the bid −1; prints without a quote are ignored |
| Stock volume percentile (0.5) | `/stock/{t}/unusualness` | (today's stock-volume percentile vs the ticker's own ~90 sessions − 50) / 50 |
| Short interest (0.5) | `/shorts/{t}/interest-float/v2` | squeeze fuel, 0…+1: 0.6 × min(short % of float / `high_short_float`, 1) + 0.4 × min((days to cover − 1) / 4, 1); low short interest is neutral, never negative |
| **Ownership & filings** | | |
| Insider transactions (0.5) | `/insider/{t}/ticker-flow` | (2 × insider buy $ − insider sell $) / (2 × buys + sells) over `insider_days` (90); open-market buys are rarer and count double |
| 13F holders (0.5) | `/institution/{t}/ownership` | (shares added − shares trimmed) / (added + trimmed) across holders in the latest reported quarter |
| Congressional trades (0.25) | `/congress/recent-trades` | (buy notional − sell notional) / (buys + sells) over `congress_days` (90) using the midpoint of each disclosed amount range |
| Analyst actions (0.5) | `/screener/analysts` | mean of: upgrade +1, downgrade −1, initiation at buy +0.75 / sell −0.75, maintained or reiterated buy +0.25 / sell −0.25, hold 0, over `analyst_days` (45) |
| **Calendar & catalysts** | | |
| Seasonality (0.25) | `/seasonality/{t}/monthly` | 0.5 × (share of positive closes for this calendar month − 0.5) × 2 + 0.5 × median monthly change / 5 %, clipped; needs ≥ 3 years |
| Last earnings reaction (0.5) | `/earnings/{t}`, `/stock/{t}/info` | 0.5 × (beat +1 / miss −1 vs the street estimate) + 0.5 × next-day move / 5 %, clipped, for the most recent report; also supplies the next report date |
| Unusual Whales headlines (0.5) | `/news/headlines` | mean of the feed's own sentiment labels (positive +1, negative −1, neutral 0) over the news window, major headlines counted twice; the headlines are merged into the news list |
| **Market & sector backdrop** | | |
| Market tide (0.75) | `/market/market-tide` | (net call premium − net put premium) / (|net call| + |net put|) for the whole market at the latest tick |
| Sector tide (0.5) | `/market/{sector}/sector-tide` | the same for the ticker's sector |

`qmag edge SYMBOL --rules` prints this table from the code, so it can never
drift from what runs. Endpoints that Unusual Whales sells only on its
Advanced/enterprise tiers (`/companies/*`, intel, analytics, `/stock/{t}/ownership`)
are deliberately not used; a feature whose endpoint your plan does not
include simply shows as missing with the API's own reason (`HTTP 403 …`).

**Where it acts.**

- **Checklist** — two gates when `edge.gate: true` (default): `uw_edge_coverage`
  (a score exists and coverage ≥ `min_coverage`) and `uw_edge`
  (score ≥ `threshold`). Both fail closed. With `edge.gate: false` the score is
  advisory: recorded, ranked on, shown, never enforced.
- **Ranking** — the score enters the composite context score with
  `edge.rank_weight` (default 1.0, alongside news 1.0 / social 0.6 / flow 1.0).
- **Rationale** — "Unusual Whales edge score +0.41 (threshold +0.15; 20/22
  features answered, coverage 91 %). For: unusual options flow +0.78, gamma
  regime & walls +0.90 … Against: max pain −0.21 … PASSES the entry
  threshold." The top contributors go into the bull and bear cases; a failed
  gate adds an explicit REJECTED line.
- **Data gaps** — every plan lists the features that did not answer and why:
  "Unusual Whales features unavailable (scored as missing, not as neutral):
  Dark pool prints (HTTP 403 Insufficient privileges); …".
- **Dashboard** — header pill `uw edge · on · gate ≥ +0.15` (or `gate not armed (no key)`: without an Unusual Whales key the score cannot exist, so the gate is not applied and every plan's data gaps say so), a `UW edge` cell
  with pass/fail in every context strip, and a per-feature panel on plan and
  lookup pages: reading, sub-score gauge, weight, contribution, status.
- **`/status`** — one connection for the score and one per feature (group
  *Unusual Whales edge features*) with its weight, rule, last error and how
  many of the last cycle's symbols it answered.
- **LLM reviewer** — the bundle gains `unusual_whales_edge` with the score,
  threshold, coverage, pass flag and every feature's score / weight / reading.

**Cost and caching.** The edge runs for the top `edge.max_symbols_per_cycle`
(20) candidates by rank, after the flow scan (which has its own cap, 25).
Roughly 20 requests per candidate on the first read of a day, about half that
afterwards: daily facts (OI change, greeks, skew, max pain, vol stats, short
interest, insiders, 13F, congress, analysts, seasonality, earnings, info) are
cached for `edge.daily_cache_hours` (12), intraday reads for
`context.cache_minutes` (30), market and sector tide for 5 minutes shared by
every symbol, the spot quote never. All requests share one process-wide
throttle (`UNUSUAL_WHALES_RPM`, default 100/min) with the flow scan and the
price feed, and a 429 is retried honouring `Retry-After`.

```yaml
edge:
  enabled: true
  gate: true               # false = advisory (ranking and display only)
  threshold: 0.15          # weighted score a candidate must reach
  min_coverage: 0.5        # share of applicable weight that must have answered
  rank_weight: 1.0         # share of the composite context score
  max_symbols_per_cycle: 20
  insider_days: 90
  congress_days: 90
  analyst_days: 45
  dark_pool_min_premium: 100000
  high_short_float: 0.15
  daily_cache_hours: 12
  weights:                 # 0 switches a feature off (its endpoint is never called)
    flow: 1.5
    net_premium: 1.0
    dark_pool: 0.75
    congress: 0            # e.g. ignore congressional trades
```

Any key can be set in the strategy YAML at any depth (a partial `weights`
map is merged over the defaults). Check one name from the shell:

```bash
qmag edge NVDA            # every feature, its reading and sub-score, the total vs the threshold
qmag edge NVDA --json     # the same breakdown the plan, dashboard and LLM bundle carry
qmag edge NVDA --rules    # the scoring rules, straight from the code
```

**Rationale.** The written justification quotes these facts: "gapped +53 % on
11.9× its 50-day volume from a neglected base… float 850 K (low float), short
interest 15 %… catalysts: offering… unavailable sources: reddit,
unusual_whales". Low float plus high short interest is called out in the bull
case (fuel) and dilution/offering headlines in the bear case.

**Analyst committee** (off by default). With `committee.enabled: true` every
plan that passes the checklist is put to **three separate model calls**, in
order: the **bull seat** argues for the trade from the plan, rationale and
trimmed context; the **bear seat** argues against it *after reading the bull
case* (`committee.debate: true`; off, the two write blind); the **risk
chair** reads both cases and rules `take | reduce | reject` with a
confidence and a size multiplier, naming the decisive factor. Each seat has
its own `committee.<seat>_provider` / `_model` on the settings page
(Language models → Analyst committee, one card per seat; the provider list
is whatever you added under **AI providers**), so the seats can come from
different vendors — a model tends to agree with its own earlier reasoning,
so a Gemini bull, a Claude or Grok bear and the strongest model you have as
chair gives a more honest argument than one model wearing three hats. The structure is the one
[TradingAgents](https://github.com/TauricResearch/TradingAgents) uses; what
changed is that it is now three models, not one model role-playing three
people. `committee.max_plans_per_cycle` (8) caps the calls a cycle can make
(three per plan) and `committee.timeout_seconds` (60) bounds each. A seat
that fails leaves an error in its place, never an invented case; without a
ruling the plan keeps its rules-engine verdict and says so. The cases and
ruling are shown on the plan page; set `committee.can_veto: true` to let
the chair cut size or block a trade. The rule-based checklist always runs
first, so the models can only make the system *more* selective. Older
`context.llm_enabled` / `llm_model` / `llm_can_veto` settings are migrated
on load (all three seats pinned to that one OpenAI model).

### Additive LLM reviewer (off by default)

A second, independent opinion modelled on
[ai-trading-agent-gemini](https://github.com/danilobatson/ai-trading-agent-gemini):
rather than asking a model to *find* trades, the trader hands it the **whole
edge bundle** for one candidate and demands a strict-JSON verdict. The bundle
([`src/qmag/reviewer.py`](src/qmag/reviewer.py), `build_edge_bundle`) is
everything the engine itself saw and nothing more:

- setup geometry (type, pivot, flag length/depth/contraction, gap, relative volume, score, theme rank),
- the sized plan (entry, stop and stop %, partial target and quantity, shares, position value and % of equity, risk $ and %, the adaptive risk multiplier, day-N partial rule, trailing MA, max hold, how the entry will be placed),
- the pass/fail checklist and the written rationale (entry / size / stop / profit plan / context / bull / bear / verdict),
- news headlines with scores and catalyst tags, next earnings date, days since the last print,
- social score, message counts, bullish/bearish tallies and sample posts,
- options-flow premium, alerts and sweeps,
- the Unusual Whales edge score with threshold, coverage, pass flag, every feature's score / weight / reading and the list of features that did not answer,
- fundamentals (sector, industry, market cap, float, short %, insider and institutional %, analyst recommendation and target),
- the committee's bull/bear/risk debate if it ran,
- the market regime and the portfolio state (equity, open and pending positions, held symbols, the last ten realised R multiples, current risk multiplier, broker).

The model must answer with exactly:

```json
{"action": "BUY | SELL | HOLD", "confidence": 0.0-1.0, "thesis": "...",
 "catalysts": ["..."], "risks": ["..."], "invalidation": "...", "sizeNote": "...",
 "sizeMultiplier": 0.0-1.0}
```

Two transports are built in. **Gemini** is called natively with
`responseMimeType: application/json` and a response schema, so the JSON
shape is enforced server-side. Any **OpenAI-compatible** endpoint (OpenAI,
Anthropic, xAI, Groq, OpenRouter, DeepSeek, Mistral, Together, Ollama,
vLLM…) is called on `/chat/completions` with `response_format:
json_object` (Anthropic's endpoint also gets its `x-api-key` /
`anthropic-version` headers). Which vendor and key a seat uses is its
**provider**, chosen from the AI providers you added on the settings page
(`auto` = the first one with a key: Gemini, then OpenAI, then the rest in
saved order). Replies are
normalised anyway: percent confidences, lower-case actions, fenced JSON and
semicolon-separated lists are all accepted; anything else is recorded as an
error and never raises inside a trading cycle.

```yaml
reviewer:
  enabled: true
  provider: auto          # gemini | openai | auto (gemini if a key is present, else openai)
  model: gemini-3.5-flash # default per provider: gemini-3.5-flash / gpt-4o-mini (the settings page lists what your key can use)
  mode: gate              # advisory | gate | gate_and_size
  min_confidence: 0.6     # gate modes: BUY below this is treated as HOLD
  fail_closed: false      # gate modes: block trades when the model is unreachable
  review_watchlist: true  # also review buy-stop candidates, not only same-day triggers
```

How the automated trader takes the verdict into account is `mode`:

| mode | effect on the trade |
| --- | --- |
| `advisory` | Verdict is recorded on the plan, logged (`REVIEW SYM: HOLD 72% …`) and shown on the dashboard; the rules alone decide. |
| `gate` | Adds an `llm_reviewer` check: only `BUY` with `confidence ≥ min_confidence` passes. `SELL`/`HOLD` or low confidence moves the plan to *Blocked setups* with the model's reason. |
| `gate_and_size` | As `gate`, and a `BUY` with `sizeMultiplier < 1` scales shares, partial quantity, risk and position value down accordingly. |

The reviewer runs *after* the deterministic checklist and the committee, so it
sees only plans the rules already accept and can only make the system more
selective (it never enlarges a position or overrides a failed rule). It applies
identically to every broker — paper ledger, Alpaca paper/live, IBKR paper/live
and MetaTrader 5 demo/live — because it sits in the plan stage, before any
order is routed. The dashboard shows the verdict on every plan card and a full
panel (thesis, catalysts, risks, invalidation, size note, provider/model) on the
plan and symbol-lookup pages; the header pill shows the active mode and whether
an API key was found. To try it on one ticker without trading:

```bash
qmag review NVDA                       # verdict table (uses the config's mode)
qmag review NVDA --mode gate_and_size  # see what the trader would do with it
qmag review NVDA --bundle-only         # print the edge bundle, no API call
qmag review NVDA --json                # raw verdict JSON
```

Ideas borrowed while reading the reference projects: news-headline sentiment
pipelines and catalyst tagging (sentiment-scanner, NewsQuant,
NEWS_SENTIMENTS_ANALYSER, StockSage), the finviz + Yahoo news combination
(daily_stock_analysis, stocksight), StockTwits bullish/bearish labels
(Stock-Sentiment-Analyzer, SentimentPulse), FinBERT as an optional scorer
(finBERT, FinGPT), and multi-agent debate with a decision journal and
reflection on realised outcomes (TradingAgents, ai-trading-agent-gemini) —
here as the adaptive-risk loop over the trade journal.

## Walk-forward optimisation

```bash
qmag optimize --start 2019-01-01 --folds 4 --objective calmar --grid grid.yaml
```

`grid.yaml` maps dotted parameter names to candidate values:

```yaml
management.trail_ma: [10, 20]
management.stop_adr_mult: [0.75, 1.0, 1.5]
management.partial_after_days: [3, 5]
themes.min_theme_percentile: [0.0, 0.3, 0.5]   # 0.0 = theme gate off
regime.min_breadth: [0.0, 0.4]                 # 0.0 = breadth gate off
regime.ma_length: [10, 20, 50]
```

The default grid (`trail_ma × stop_adr_mult × min_theme_percentile ×
min_breadth`, 36 combinations) takes a few minutes per fold on ~80 symbols.

For each fold the best in-sample combination is re-run on the following
out-of-sample window and compared with the untouched defaults. If `oos_return`
does not resemble `is_return`, the parameters are fitted to noise — that
comparison is the point of the command, not the top row.

## Paper / live trading

```bash
# Local ledger, no credentials
qmag paper run
qmag paper status

# Alpaca paper account
pip install -e ".[alpaca]"
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...
qmag paper run --broker alpaca

# Interactive Brokers (TWS or IB Gateway running with the API enabled)
pip install -e ".[ibkr]"
export IBKR_HOST=127.0.0.1 IBKR_PORT=7497 IBKR_CLIENT_ID=17   # 7497 TWS paper, 4002 Gateway paper
qmag paper run --broker ibkr

# IBKR for both data and execution ('--data auto' already picks IB while the gateway answers;
# with '--data ibkr' a gateway outage serves that load from Yahoo and flags it on /status,
# or fails the load when IBKR_DATA_FALLBACK=no)
qmag paper run --data ibkr --broker ibkr

# Headless IB Gateway on a Linux server (x86-64 or ARM64), auto-login via IBC:
#   bash deploy/install-ibgateway.sh          # installs Docker, writes ~/ibgateway/.env for the PAPER login
#   bash deploy/install-ibgateway.sh          # starts ghcr.io/gnzsnz/ib-gateway on 127.0.0.1:4002 (paper) / 4001 (live)
#   bash deploy/install-ibgateway.sh --wire   # saves IBKR_* to the desk, runs broker-test, switches the systemd units to --broker ibkr

# MetaTrader 5 (Windows, terminal running and logged in; demo account for `mt5`)
pip install -e ".[mt5]"
export MT5_SYMBOL_SUFFIX=.US MT5_LOGIN=... MT5_PASSWORD=... MT5_SERVER=...
qmag paper run --data mt5 --broker mt5

# Live: alpaca-live / ibkr-live (ports 7496 / 4001) / mt5-live. Asks for
# confirmation unless --yes-live is passed. Run paper for months first.
qmag paper run --broker alpaca-live
```

| Broker | Entry (watchlist) | Entry (triggered) | Exits |
| --- | --- | --- | --- |
| `paper` | buy-stop-limit bracket, filled from the daily bar | market at last close | OCO (limit target / stop) + stop; fills are computed conservatively from the real bar (stop before target on a bar touching both) |
| `alpaca` / `alpaca-live` | `StopLimitOrderRequest` bracket | market | `OrderClass.OCO` + GTC stop |
| `ibkr` / `ibkr-live` | STP LMT parent + attached STP child | market | OCA group (LMT + STP, `ocaType=1`) + GTC stop |
| `mt5` / `mt5-live` | `BUY_STOP_LIMIT` pending order with SL | `TRADE_ACTION_DEAL` | protective stop as the position's SL; the +2R partial is a pending `SELL_LIMIT` — when it fills the SL keeps protecting the rest |

MT5 notes: quantities are shares and are converted to lots through the
symbol's `trade_contract_size` / `volume_step`; only orders and positions
carrying `MT5_MAGIC` are touched; `--broker mt5` refuses to run against a
terminal logged into a real account (use `mt5-live`). Netting and hedging
accounts are both handled.

Trader state (why you hold something, its initial stop, target, whether the
partial is done) is kept in `paper_state/trader.json`; the broker only knows
quantity and price. Anything held at the broker that the trader did not buy is
left alone and flagged.

### IBKR: paper trading and market data, step by step

One IB Gateway login gives qmag two things: an order-routing venue for
`--broker ibkr` (paper) / `ibkr-live`, and a **daily-bar feed** that
`--data auto` uses in preference to Unusual Whales and Yahoo whenever the
gateway is up. This section takes you from an IBKR account to a desk that
scans on IB bars and trades the IB paper account, headless on a Linux
server. Nothing here touches a live account: everything below is the paper
login on the paper port.

**What you get, honestly**

* *Price data.* With `IBKR_PREFER_DATA=yes` (default) and the data source on
  `auto`, every scan, chart, probe and refresh reads daily bars from IB and
  Unusual Whales calls are spent only on what it alone provides: options
  flow, the 22-feature edge score, screeners, dark pool, insiders. Without
  a paid US-stock market-data subscription IB serves **delayed** bars
  (15-20 min; qmag requests market data type 3 for exactly this reason) —
  *provided the account has US-stock market-data permission at all*. That
  permission comes from the live account: it must be approved for US stock
  trading, and *Share real-time market data with paper trading account*
  must be ticked. A brand-new paper account whose live account is not yet
  funded / permissioned gets **no bars at all**, not even delayed (IB
  answers `error 162: No data of type EODChart is available for the
  exchange 'BEST'` or `No Route Found`). qmag detects that with a single
  request, serves the load from Unusual Whales / Yahoo, writes *IBKR bars
  set aside for now: ...* into the price-data status and tries IB again an
  hour later — orders keep going to IB meanwhile.
  For an end-of-day strategy delayed bars are not a limitation: the bar
  the nightly scan needs is final long before the scan runs, and the
  intraday passes are looking for gaps and triggers that are still there
  twenty minutes later. If you subscribe (e.g. *US Securities Snapshot and
  Futures Value Bundle*, about USD 10/month, or the *NASDAQ / NYSE Network*
  feeds) and share it with the paper account as above, you get real-time
  bars with no change in configuration.
* *Volume.* Some gateway builds report US stock volume in lots of 100 and
  some in shares. Rather than guess, qmag measures the scale once against
  Yahoo's SPY volume (`data/cache/ibkr/ibkr_volume_scale.json`, remeasured
  weekly). If it cannot measure it (Yahoo down on the very first load) the
  load is served by Yahoo and the report says so; nothing is written with a
  volume that might be 100× off. `IBKR_VOLUME_MULTIPLIER=1|100` forces it.
  Separately — and this is inherent to IB, not a scale problem — IB's
  historical volume counts **lit-exchange prints only** (no TRF / dark-pool
  volume), so it runs at roughly 55-75 % of the consolidated figure Yahoo
  and Unusual Whales report (measured on Gateway 10.45: SPY 0.54-0.69,
  AAPL 0.67-0.75, NVDA 0.55). Prices match to the cent. Everything
  *relative* (breakout volume vs its 50-day average, EP volume multiples,
  RVOL) is unaffected because both sides come from the same source; the
  one *absolute* test, the `$5M/day` liquidity floor, becomes effectively
  stricter (≈ $7-9M consolidated). The status line says *IB volume is
  lit-exchange only (~65 % of consolidated)* so you know which regime you
  are in; lower `momentum.min_dollar_volume` a notch (e.g. 3.5M) if you
  want the same breadth as on Yahoo bars.
* *Coverage.* IB has no security definition for a few hundred of the ~2,900
  liquid names (some OTC-adjacent ADRs, very recent listings, some units /
  warrants). Those are loaded from Yahoo, reported as
  `provider.fallback = {source: yfinance, requested, loaded}` in the run's
  data stats and on `/status`. Set `IBKR_DATA_FALLBACK=no` to leave them out.
* *Speed.* Requests run 8 at a time (`IBKR_DATA_CONCURRENCY`). The first
  whole-market cold load takes about 10-15 minutes on a small VM (2,900
  contracts, one 400-day request each); after that refreshes are
  incremental — the nightly scan fetches a week of bars per name and the
  five-minute passes only touch the shortlist. IB's historical pacing limits
  apply to *small* bars; daily bars at this rate have not tripped them.
* *Availability.* The gateway restarts itself nightly (11:45 pm New York)
  and IB forces a weekly re-login (Sunday) — a paper login without two-factor
  reconnects automatically. While the gateway is down `auto` falls back to
  Unusual Whales / Yahoo for that load (the status line names the source),
  and the trader will not place IB orders until it is back.
* *What it does not do.* IB's historical bars are split-adjusted but not
  dividend-adjusted; Yahoo's are both. That is why IB bars live in
  `data/cache/ibkr/` and are never merged with Yahoo's. Momentum ranks and
  ADR are unaffected; over a multi-year backtest a high-dividend name looks
  a few percent different — use `--data yfinance` for long backtests if
  that matters to you.

**Step 1 — IBKR account and paper login**

1. You need a funded (or at least approved) IBKR account. Client Portal →
   Settings → Account Settings → *Paper Trading Account* creates the paper
   login; note the **paper username** (usually your username with a suffix,
   e.g. `edemo123`) and set its password. The paper account starts with
   $1,000,000 — Client Portal lets you reset it to something realistic; do
   that so position sizing (0.5 % risk, 25 % max per name) looks like your
   real account.
2. In the same *Paper Trading Account* page tick **Share real-time market
   data with paper trading account** if you have any subscriptions. Without
   it you get delayed bars, which is fine (see above).
3. Two-factor: the paper login can use the same IB Key as the live login. A
   headless gateway cannot answer a phone prompt, so for the *paper* login
   either accept the weekly prompt on your phone when the log asks for it
   (the container waits, then retries), or in Client Portal → Security →
   *Secure Login System* opt the **paper** user out of two-factor (IB allows
   this only for paper, and only with trading permissions reduced to what a
   paper account has anyway).

**Step 2 — install the headless gateway on the server**

The repository ships a Docker Compose stack for
`ghcr.io/gnzsnz/ib-gateway:stable` (IB Gateway + IBC auto-login + a virtual
display; native images for x86-64 and ARM64, e.g. Oracle Ampere VMs).

```bash
# on the server, in the qmag checkout
bash deploy/install-ibgateway.sh
#   first run: installs Docker + compose, creates ~/ibgateway/{docker-compose.yml,.env,tws_password}
#   and stops so you can fill in the credentials
```

Put the **paper** username in `~/ibgateway/.env` (`TWS_USERID=...`) and the
password, and nothing else, in `~/ibgateway/tws_password` (mode 0600 — the
installer created it empty). From a Windows PC that already has SSH access
to the server:

```powershell
$u = Read-Host "IBKR paper username"
$p = Read-Host "IBKR paper password" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($plain))
ssh -i $env:USERPROFILE\.ssh\<your-key> ubuntu@<server> "sed -i 's/^TWS_USERID=.*/TWS_USERID=$u/' ~/ibgateway/.env && sudo rm -f ~/ibgateway/tws_password && umask 077 && echo $b64 | base64 -d > ~/ibgateway/tws_password && echo saved"
```

(base64 in transit so `&`, `$`, quotes and spaces in the password survive
the shells; `sudo rm` because after the first start the file belongs to the
container's user.) Then start it:

```bash
bash deploy/install-ibgateway.sh          # second run: validates the credentials, pulls the image, fixes file ownership,
                                          # starts the container, waits for 'Login has completed', restarts once (see below)
ss -ltn | grep -E '4002|4001'            # 127.0.0.1:4002 = paper API
cd ~/ibgateway && docker compose logs -f  # if you want to watch: 2FA prompts and login errors show up here
```

Two things the installer handles that bite when done by hand: the image
runs as uid 1000 while the host user is often uid 1001, so the password
file and the settings volume are chowned to the container's uid (otherwise
the container restarts forever with `Permission denied`); and IBC unticks
*Read-Only API* through the gateway's settings dialog on the first login,
but the gateway only honours that after a restart, so the installer
restarts it once and remembers (`~/ibgateway/.settings-initialised`).

Compose settings worth knowing (`deploy/ibgateway/docker-compose.yml`):
`TRADING_MODE=paper`, `READ_ONLY_API=no` (orders allowed), API ports bound
to `127.0.0.1` only, `AUTO_RESTART_TIME=11:45 PM` New York,
`TWOFA_TIMEOUT_ACTION=restart`, a 1.5 GB memory cap and a 768 MB Java heap
(comfortable on a 6 GB VM next to qmag), VNC on `127.0.0.1:5900` if you ever
need to see the gateway window (`ssh -L 5900:127.0.0.1:5900 ...`).

**Step 3 — wire qmag to it**

```bash
bash deploy/install-ibgateway.sh --wire
```

This installs `ib_async` into the desk's virtualenv, saves
`IBKR_HOST=127.0.0.1 IBKR_PORT=4002 IBKR_CLIENT_ID=17 IBKR_DATA_CLIENT_ID=18`
to `paper_state/settings.env`, checks the port, runs `qmag status --probe
--broker ibkr`, then **proves order routing** with `qmag broker-test --broker
ibkr --kind bracket` (a far-away 1-share bracket placed, listed and
cancelled on the paper account) and only if that passes rewrites the
`qmag-daemon` / `qmag-dashboard` systemd units to `--broker ibkr` and
restarts them. Code updates through `deploy/push-vm.sh` keep the broker
you have set.

Nothing else is needed for the data side: with the gateway answering on
`127.0.0.1:4002` and the data source on `auto`, the next cycle logs
`price data source: unusual_whales -> ibkr` (or `yfinance -> ibkr`) and
`/status` → *Price data* reads *IBKR TWS/Gateway 127.0.0.1:4002 daily bars
(delayed unless ...)*. The first cycle after the switch is the cold load
(10-15 minutes); the desk keeps serving the previous report meanwhile.

**Step 4 — check it is really IB**

```bash
qmag status --probe                 # Price data: IBKR ... latest bar <yesterday or today>, latency
qmag scan --symbols SPY,AAPL --data ibkr
journalctl -u qmag-daemon -n 300 | grep -E 'price data source|IBKR|fallback'
```

On the dashboard's `/status` page the header pill reads `ibkr · <broker>`
and the *Price data* row's detail is written from the last load:
`ibkr: 2861/2863 symbols loaded; latest bar 2026-09-11; 214/220 symbols IB
could not serve came from yfinance; IB volume x100; IB said 'error 200: No
security definition has been found for the request' for 220 symbols`. When
the gateway was down for a load it says `IBKR unavailable
(ConnectionRefusedError: ...) - load served by yfinance` and the row is
marked degraded. The full IB error summary (e.g. *error 162: ... No market
data permissions* when a feed needs a subscription) is logged as `IBKR
error summary:` in the daemon / dashboard journal after every load.

**Without a server (laptop, TWS)**

The same works against TWS or IB Gateway on your own machine: enable the
API in TWS (*Global Configuration → API → Settings → Enable ActiveX and
Socket Clients*, untick *Read-Only API*, port 7497 for paper), then

```bash
pip install -e ".[ibkr]"
export IBKR_HOST=127.0.0.1 IBKR_PORT=7497
qmag paper run --broker ibkr            # data source 'auto' picks IB because 7497 answers
qmag dashboard --broker ibkr
```

**Troubleshooting**

| Symptom | Cause / fix |
| --- | --- |
| Log stops at `Second Factor Authentication` / `2FA timeout` | Approve the IB Key prompt on your phone; the container retries every `TWOFA_TIMEOUT_ACTION` cycle. For hands-off operation opt the paper user out of two-factor (Step 1.3). |
| `Login failed` / `invalid username or password` | The **paper** username differs from the live one. Check `~/ibgateway/.env` and re-write `tws_password` (no trailing newline is fine; the file is read verbatim). |
| Port 4002 never opens | `docker compose logs` — usually still logging in, or `TRADING_MODE` is not `paper` (a live login exposes 4001). |
| Container restarts every ~20 s; log shows `common.sh: ... /run/secrets/tws_password: Permission denied` or `tws_settings/jts.ini: Permission denied` | The files are not readable by the container's uid. Re-run `bash deploy/install-ibgateway.sh` (it chowns both), or by hand: `sudo chown 1000 ~/ibgateway/tws_password` and `docker run --rm --user 0 -v qmag-ibkr_tws_settings:/s --entrypoint chown ghcr.io/gnzsnz/ib-gateway:stable -R 1000:1000 /s`. |
| Orders rejected with `Warning 321 ... The API interface is currently in Read-Only mode` although `READ_ONLY_API=no` | IBC unticked the box on this login; the gateway applies it on the next start. `cd ~/ibgateway && docker compose restart`, wait for `Login has completed`, re-run `--wire`. |
| Price-data status says *IBKR bars set aside for now: IB returned no SPY bars (error 162: ... No data of type EODChart ... / No Route Found)* | The account has no US-stock market-data permission at all (typical for a paper account whose live account is new / unfunded / not permissioned for US stocks). In Client Portal: fund or complete the live account, check *Settings → Trading Permissions → United States → Stocks*, tick *Paper Trading Account → Share real-time market data*; optionally subscribe to *US Securities Snapshot and Futures Value Bundle*. qmag retries IB every hour and serves Unusual Whales / Yahoo bars meanwhile; orders still go to IB. |
| Account equity looks like a EUR / GBP figure | IB reports equity in the account's base currency. qmag converts it to USD as soon as IB publishes a USD exchange rate for the account (after the first USD position); until then sizing uses the base-currency figure as if it were dollars and the log says so. Set the paper account's base currency to USD in Client Portal if you want to avoid the gap entirely. |
| `qmag broker-test` fails at *connect* with client id in use | Another process holds client id 17 / 18. Stop the other desk or change `IBKR_CLIENT_ID` / `IBKR_DATA_CLIENT_ID` on the settings page. |
| Price data status: *nothing listening on 127.0.0.1:4002* | Gateway down (nightly restart takes ~1 minute; weekly re-login longer). `auto` serves that load from Unusual Whales / Yahoo and switches back by itself. |
| Status detail says `IB said 'error 162: ... No market data permissions'` | Bars for that exchange need a subscription even delayed; those names are served by Yahoo (the `came from yfinance` count). |
| Status detail says `IB said 'error 162: ... pacing violation'` | Lower `IBKR_DATA_CONCURRENCY` (e.g. 4) on the settings page; the affected names were served by Yahoo for that load and are retried next time. |
| Report says `ibkr_error: could not verify IBKR's volume scale` | Yahoo could not be reached on the very first IB load; the load was served by Yahoo. Retry later, or set `IBKR_VOLUME_MULTIPLIER` after checking one name's volume against any quote page. |
| Volumes on charts look 100× too small / large | Delete `data/cache/ibkr/ibkr_volume_scale.json` and `data/cache/ibkr/*.csv`, or set `IBKR_VOLUME_MULTIPLIER` explicitly, then re-run. |
| Want Unusual Whales / Yahoo bars back | Settings → Price data → *Use IB for price data when the gateway is up* = `no` (keeps IB for orders), or choose a source explicitly in *Data source*. |
| Want the whole desk back on the local paper ledger | `BROKER=paper bash deploy/install-vm.sh` (units back to `--broker paper`); `cd ~/ibgateway && docker compose down` stops the gateway. |

Going live later is the usual three-level opt-in (`--broker ibkr-live`,
`--yes-live`, `QMAG_YES_LIVE=1`) plus a *live* gateway login
(`TRADING_MODE=live`, port 4001) — a separate decision, deliberately not
covered by `--wire`.

### Several accounts: one desk per account

qmag can drive as many accounts as you like — Alpaca paper, an IBKR paper
account, a second IBKR login, a MetaTrader account, a local paper ledger —
but it does **not** fan one order out to several accounts. Each account gets
its own **desk**: a state directory with its own daemon, journal, settings,
credentials, risk budget and kill switch. The reasons are practical:

* Fills, cash, base currency (EUR at IB, USD at Alpaca), permissions and
  slippage differ per account. Sizing 0.5 % of *that* account's equity and
  anchoring the stop on *that* fill is only possible per account; a mirrored
  order that fills on one account and not on the other would leave the book
  wrong on one of them.
* One writer per book. Every desk reconciles its `trader.json` against its
  own broker each pass; two accounts behind one book would break that
  invariant (see [Invariants](#9-invariants---do-not-break-these)).
* The daily loss circuit-breaker, the heat cap and the halt switch are
  per-account decisions.

What you get across desks is one view and shared control:

* **`/accounts`** on any desk's dashboard shows every account (this desk plus
  the ones listed in `QMAG_DESKS`) with equity, cash, holdings marked at the
  latest real bar, unrealised and realised P&L, orders, daemon heartbeat and
  kill switch — summed per currency. `qmag accounts [--refresh] [--json]` prints
  the same from the CLI.
* **Kill switch per account** from that page: throwing another desk's switch
  writes its `halt.json`; its daemon stops opening positions on its next pass.
  Flattening (selling at market) needs that desk's broker connection and is
  offered on the desk itself.
* A **watch-only** account is a desk with its switch thrown: its holdings and
  P&L show up, nothing is traded.

How it works: after every cycle a desk writes `account.json` — the broker's
own account read (`NetLiquidation` / `TotalCashValue` at IB, the account
endpoint at Alpaca, the ledger for paper), every position the broker reports
(managed by qmag or not), each marked at the last real close from the bars the
cycle loaded (holdings outside the scan are fetched from the desk's price
source; a holding without a fresh bar shows **no** market value rather than an
estimate), resting orders, and realised P&L from the journal. The accounts
page reads those files; a snapshot older than eight hours is flagged **STALE**
and left out of the totals, a desk whose broker could not be read shows the
error instead of stale numbers, and accounts in different currencies are
totalled separately.

Adding an account on the VM:

```bash
# on the host, as the install user
bash deploy/add-desk.sh alpaca --broker alpaca --port 8856
```

This creates `~/qmag/desks/alpaca`, copies the main desk's `settings.yaml`
as a starting strategy, installs and starts `qmag-daemon-alpaca` and
`qmag-dashboard-alpaca` (on `127.0.0.1:8856`), and lists the new desk in the
main desk's `QMAG_DESKS` (and the main desk in the new one's). Then:

1. Put **that** account's credentials on the new desk's settings page
   (`ssh -L 8856:127.0.0.1:8856 ubuntu@HOST`, then `http://127.0.0.1:8856/settings`)
   or in `desks/alpaca/settings.env`. Credentials are per desk; nothing is
   copied from the main desk.
2. `qmag broker-test --broker alpaca --state-dir ~/qmag/desks/alpaca` proves
   order routing on that account.
3. The main dashboard's **accounts** page shows it after its first cycle.

Notes per broker:

* **Second IBKR login**: one IB Gateway serves one login. Copy
  `deploy/ibgateway` to another directory with different host ports
  (`4011:4001`, `4012:4002`), give the new desk `IBKR_PORT=4012` and client ids
  that differ from the main desk's (`IBKR_CLIENT_ID`, `IBKR_DATA_CLIENT_ID`).
  Two *sub-accounts* under one login are not selected today; use one desk per
  login.
* **Alpaca**: paper and live keys are different pairs; `--broker alpaca` is
  paper, `alpaca-live` is live and needs `QMAG_YES_LIVE=1` like everywhere
  else.
* **Anywhere else** (another machine, a laptop): run
  `qmag daemon --broker … --state-dir /path` there, share or sync the state
  directory, and add `name=/path|http://host:port` to **Other desks to show**
  on the settings page.

Removing a desk: `sudo systemctl disable --now qmag-daemon-NAME qmag-dashboard-NAME`,
delete the two unit files, `daemon-reload`, and take it out of `QMAG_DESKS`.

### Running 24/7: the tiered schedule

```bash
qmag daemon --broker alpaca                # paper Alpaca, whole-market universe
qmag daemon --show-schedule                # what will run in the next 7 days
qmag daemon --once focused                 # run one task now: premarket | post_open | focused | movers | after_close | insider_scan | learn | universe
qmag paper run --focused                   # the same focused pass from the CLI
qmag dashboard --port 8765                 # in a second process
```

`qmag daemon` is the fully automated mode: research, arming, triggering,
sizing, orders, stops, targets and exits all happen on the schedule below
with nothing to click. It is **tiered** so the expensive work (a
whole-universe scan plus news / social / options / LLM context for every
candidate) runs once a day, while the fast work (is anything on the shortlist
breaking out *right now*?) runs every few minutes on a small set of names:

| When (America/New_York) | Task | What it looks at |
| --- | --- | --- |
| 16:20 (13:20 early close) | **after-close full scan** | every symbol in the universe: fresh bars, regime + breadth, theme ranks, setups, full checklist and context for the candidates, buy-stop brackets for tomorrow, charts. Rebuilds the **arming list**: every flag within `schedule.arming_distance_pct` (5 %) of its pivot plus anything that fired, ranked by score, capped at `arming_max_names` (60). |
| 08:30, 09:20 | **pre-market gap screen** | the market-wide screener (Unusual Whales): stocks gapping ≥ `premarket_min_gap_pct` (8 %) are *armed* with source `premarket` so the first focused passes look at them. No bars exist for today yet, so nothing is detected or traded here. |
| 09:40 | **post-open full scan** | the whole universe once more on the first real bars (episodic pivots gap-and-go here). Optional (`post_open_full_scan`). |
| 09:35 → 15:55, every 5 min | **focused pass** | only the arming list + open positions + resting entries (+ screener hits). Today's partial volume is projected to full-day **pace** (`pace.py`, the U-shaped intraday profile) so a breakout at 10:30 on a quarter of the day's normal volume is not called "no volume"; the projection is labelled as such in the log. Plans are built only for names within `trigger_distance_pct` (2 %) of the pivot; farther ones are left alone with their buy-stops untouched. Unchanged plans are **kept** (no order churn); changed ones replaced; dead setups cancelled. Breadth is inherited from the last full scan — with no full scan in the last 4 sessions the regime is *unknown* and the pass fails closed. |
| 10:00 → 15:30, every 30 min | **movers sweep** | the screener again: stocks up ≥ `movers_min_change_pct` (8 %) on ≥ `movers_min_rvol` (3×) volume that are not on the arming list. Hits are added to that pass's scope; only a hit that shows a *valid setup on real bars* is armed / traded — a screener row on its own never places an order. |
| Saturday 10:00 | **insider / unusual-options scan** | see the next section. |
| Saturday 11:00 | **learning review** | the trade journal and the shadow ledger are reviewed; lessons are written and the selection / trigger knobs are nudged within their guard rails when the evidence clears the bar (see *Learning from its own trades*). |
| Sunday 12:00 | universe rebuild | `universe/market.txt` + `universe/fundamentals.csv`. |

Everything in the table is configurable on the settings page under **Scan
schedule (tiered)**; `schedule.tiered: false` restores the old timetable
(09:45 / every 30 min / after close, all full scans). The dashboard shows the
arming list, which pass produced the last report (`focused 41` in the header
pill) and the schedule mode.

Why this shape: the nightly scan cannot miss a setup because it looks at
everything; the pre-market and movers screens catch the names that were not
set up last night (gaps, news); and the 5-minute passes spend the API and LLM
budget only on the handful of names that can actually trigger in the next
few minutes. The screener needs `UNUSUAL_WHALES_API_KEY` (the free finviz
screener is used for movers when the key is missing; it has no pre-market
filter, so the pre-market screen is then recorded as *not configured* rather
than guessed).

### Saturday: who took an unusual risk this week?

Every Saturday at 10:00 NY (`insider_scan.weekday` / `run_time`) the daemon
reviews the past 7 days of **market-wide unusual options activity** from
Unusual Whales to find trades that fit the profile seen **the day before**
takeovers, FDA decisions and guidance shocks, and asks the AI what they
could be positioning for. The profile it hunts for is the one from the
famous cases — Zendesk's $70 calls bought 24 % out of the money the day
before its $77.50 buyout, GoPro's short-dated calls at 50× normal volume
before its merger, Heinz's June $65 calls with almost no prior open
interest the day before Buffett — namely: **out-of-the-money calls (or
puts), a few weeks to expiry, bought aggressively (sweeps, at the ask,
opening trades), concentrated in one strike / expiry, in a chain that is
normally quiet, with no scheduled event inside the contracts' life**.

1. **Pull** the week's flow alerts (`/option-trades/flow-alerts`, paged by
   time) with the `unusual` preset plus our own bar: premium ≥ $150k,
   volume ≥ 3× open interest, ≥ 5 % out of the money, 3–45 days to expiry,
   ≥ 70 % filled at the ask; and each session's unusual-contract screen
   (`/option-activity/unusual`), filtered client-side by the same bar.
   Index / ETF products (`SPY`, `QQQ`, `TLT`…) are excluded — they are
   hedging vehicles; 0–2 DTE prints are ignored — that is day-trading.
2. **Score** each ticker's week 0–10, shape over size: premium (log,
   capped at 1.5 — a $50m block in a mega cap is routine business), volume
   vs open interest (up to 1.5), how far out of the money the **dominant
   bet** is (10–35 % is the sweet spot: 1.5; 5–10 %: 0.75; beyond 60 % is
   lottery: 0.5), its **days to expiry** (8–45: 1.5; 3–7: 1.0; 0–2: 0.25),
   urgency (sweeps, ≥ 80 % ask fills, repeated days, all-opening; up to
   1.5), one-directional conviction (1.0), **concentration** in one
   strike / expiry (≥ 60 % of the premium: 1.0), **fresh open interest**
   (0.5), that week's **chain volume vs the 30-day average** (≥ 20×: 2.0;
   ≥ 10×: 1.5; ≥ 5×: 1.0; ≥ 3×: 0.5), a **crowded-chain penalty** for
   tickers whose options trade hundreds of thousands of contracts a day
   (≥ 500k: −1.5; ≥ 200k: −1.0; ≥ 75k: −0.5 — nothing in AAPL's chain is a
   quiet tell), and whether the contracts expire **before the next
   scheduled earnings** (+1.5: no scheduled excuse) or straddle it (−1:
   ordinary event speculation). Three more components look past the
   options themselves: a **repeat buyer** — the same strike / expiry bought
   on two sessions (0.75) or three or more (1.0), someone building a
   position on purpose rather than one print that could be anyone's hedge;
   the **stock's own tape** when the bet went on, read from the desk's daily
   bars — flat (< 3 % over five sessions on normal share volume) earns 0.5
   because informed buyers move *before* the stock does, while a stock
   already up 6 % / 10 % in the bet's direction is being *chased* and loses
   0.5 / 1.0; and a **strike beyond the 52-week range** (a call more than
   5 % above the year's high, a put below its low: 0.75) — a bet on a price
   the stock has not seen in a year, the takeover-price profile, not a
   swing trade. The top `enrich_top` (40) candidates get
   two extra reads — `/stock/{t}/info` for the market cap and next
   earnings, `/stock/{t}/options-volume` for the week's volume history —
   and tickers outside `min_market_cap`..`max_market_cap` ($100m–$30bn)
   are **removed** even when the API's own filter let them through (the
   daily contract screen has none). Tickers ≥ `min_flag_score` (5.5) are
   flagged, top `max_flagged` (15); the displayed score is capped at 10 and
   the uncapped sum ranks the textbook cases among themselves. When the
   bars for a candidate cannot be loaded the stock components stay at zero
   and the card says so — nothing is assumed.
   **Scorecard.** Every scan also measures how earlier weeks' flags played
   out: from the first close after the scan week, the best move in the
   bet's direction over the next 10 sessions (bullish: highest high,
   bearish: lowest low). The page shows how many flags moved ≥ 10 % and
   ≥ 20 % and the average best move; flags whose bars are missing are
   counted as unmeasured, never guessed. It is the tool's own honesty
   check — if the hit rate is no better than chance, tighten the filters.
3. **Gather public context** for each flag: headlines (finviz + Unusual
   Whales), the earnings date, Form 4 insider filings of the last 90 days,
   sector and market cap. Whatever could not be fetched is listed as a gap.
4. **Ask the AI** ([`src/qmag/insider_scan.py`](src/qmag/insider_scan.py),
   same Gemini / OpenAI-compatible transport as the reviewer) for a
   strict-JSON read: `verdict` (investigate / likely_explained / noise),
   `suspicion` 0–1, `direction`, **what the buyer is speculating on** (the
   move, size and deadline the contracts pay off on), `possible_catalysts`
   with likelihood and basis, whether public information already explains
   it, `what_to_check` next and `risks` (ways the read could be wrong). The
   prompt forbids inventing facts and tells the model to prefer the boring
   explanation whenever the headlines or calendar supply one.

The result is `paper_state/insider_scan.json`, shown on the dashboard's
**🔎 insider scan** page (with a *Run insider scan now* button) and by
`qmag insider-scan [--no-ai] [--json]`. Every flag is a **research lead, not
an accusation**: the page says so, and nothing on it is traded automatically.
Without `UNUSUAL_WHALES_API_KEY` the scan reports *not run*; without a model
key the flags are still produced and the AI step is recorded as skipped.

### Entries: no resting orders unless you ask for them

A buy-stop resting at the broker overnight is filled by *whoever prints a
tick through the pivot* — a pre-market spike on 400 shares, a wick in the
first minute, a stop-run — and by the time the bar closes the "breakout" is
gone. So the default entry mode places **no resting orders at all**;
`entry.mode` picks one of three:

| `entry.mode` | What sits at the broker | When a buy happens |
| --- | --- | --- |
| `confirmed` (default) | nothing — the setup is **ARMED** on the watchlist | a focused pass sees the live bar **trading above the pivot** *and* every confirmation gate passes; the buy goes in **at market** with the stop and target attached. |
| `hybrid` | nothing until `entry.resting_from` (09:40); after that a buy-stop for the day, cancelled at the close | as `confirmed` for the first minutes; later a resting buy-stop may fill but only if the gates would pass. |
| `resting` | the classic GTC buy-stop bracket placed after the close | whenever price trades through the stop (the old behaviour). |

The confirmation gates (`trader.confirmation_gates`) that a triggered name
has to pass in `confirmed` / `hybrid` mode — each failure is logged as
**HOLD** with the reason and the name stays armed for the next pass:

* **holding, not poking** — the *current* price is above the pivot
  (`entry.require_hold_above_pivot`), not just the high of the bar; a
  wick-only breakout is a hold, not a buy;
* **not extended** — no more than `breakout.max_gap_pct` above the pivot; we
  do not chase;
* **outside the opening range** — nothing in the first
  `entry.opening_range_minutes` (10) of the session, where most fake
  breakouts print;
* **volume on pace** — today's partial volume, projected to the full day
  with the intraday profile, is at least `entry.confirm_volume_ratio` (1.0×)
  the 20-day average, and the projection is measurable (not in the first
  minutes). The log shows what was measured: `BUY … [confirmed: holding
  above 41.20, 2.36x projected volume]`.

Positions that *did* get in on a fake move are cut by the **failed-breakout
exit** (`entry.failed_breakout_exit`, default on): within
`failed_breakout_days` (1) of entry, if the close is back below the pivot
minus `failed_breakout_tolerance_pct` (0.5 %) and the stop is still further
down, the position is sold at market instead of waiting for the full stop.
Breakouts that held but went nowhere are handled by the **time stop**
(`management.time_stop_days`, default 5; `time_stop_min_mfe_r`, default 1.0):
after that many completed sessions a position closing at or under its entry
that never showed +1R of open profit and has not taken a partial is sold at
the close with reason `time_stop`, and the post-mortem says so. The backtester
applies the same rule, so `qmag backtest` / `qmag optimize` reflect it.
Nothing in the mode choice needs a human: the daemon arms at night, confirms
during the day, cuts the fakes, and writes every hold reason to the log.

### Learning from its own trades

The system keeps a journal detailed enough to answer *why*, then reviews it
every Saturday at 11:00 NY (`learning.review_weekday` / `review_time`, also
`qmag learn` or the **🧠 learning** page's *Run review now*):

1. **Every entry carries its features** (`ManagedPosition.features`): entry
   mode and time of day, relative volume (actual or projected), the edge
   score and coverage, theme percentile, ADR, flag depth, gap above the
   pivot, minutes since the open, regime, risk multiplier, the setup type and
   which pass produced it. While open, the position tracks its **MFE / MAE**
   in R.
2. **Every close gets a post-mortem** (`learning.explain_trade`): rule-based
   tags such as `wick_fill`, `low_volume`, `chased`, `opening_range`,
   `weak_theme`, `low_edge`, `never_worked`, `gave_back` plus a one-line
   plain-English explanation. When a model key is configured
   (`learning.llm_enabled`) the AI writes a strict-JSON post-mortem for the
   most recent trades as well; when it is not, that step is recorded as a
   gap on the connections page — never invented.
3. **Shadow trades** (`TraderState.shadow`) record what we did *not* take:
   setups **rejected** by a filter, **held** by a confirmation gate,
   **armed** but never triggered, skipped for lack of a slot, or **near
   misses** — every full scan also runs the detectors one step *looser* on
   the detector-level knobs (volume ratio, ADR floor, flag depth) and records
   whatever triggers only under the relaxed rules, tagged with the knob(s)
   that excluded it. Each shadow is followed on real bars for
   `shadow_max_days` (5) to see if it would have triggered and then
   `shadow_hold_days` (10) to see if it would have hit the target or the
   stop, and gets the same rule-based **post-mortem** as a real trade when it
   resolves. This is what tells the reviewer whether a filter is saving
   money or costing it; the learning page lists the **best trades we did not
   take** and the **worst trades we dodged** with the reason for each.
4. **The review** (`learning.review`) buckets real trades by each feature
   (win rate, average R, profit factor, expectancy) and writes **lessons** in
   plain English, e.g. *"Entries on < 1.2× volume: 7 trades, 14 % win rate,
   −0.6 R average; entries above: 11 trades, +0.9 R."* or *"Setups blocked by
   `themes.min_theme_percentile` would have made +0.7 R on average across 9
   shadows — the filter looks too tight."*
5. **Bounded adjustments.** Eight knobs may be tuned, each with a hard
   range and a step (`learning.KNOBS`): `breakout.min_breakout_volume_ratio`,
   `entry.confirm_volume_ratio`, `entry.opening_range_minutes`,
   `edge.threshold`, `themes.min_theme_percentile`, `momentum.min_adr_pct`,
   `breakout.max_flag_depth`, `breakout.max_gap_pct`. The objective is
   **total R per period**, not average R — a filter that raises the average
   by throwing away profitable trades is not an improvement. So a knob is
   **tightened** one step only when the marginal band of real trades just
   inside it has at least `min_trades // 2` (and `min_trades` = 8 overall),
   underperforms the rest by `min_lift_r` (0.25 R) after shrinkage towards
   zero, *and* loses money in total; it is **loosened** one step when the
   shadows that *only* that knob blocked would have made `min_lift_r` more
   than the real trades. One step per knob per review, a 14-day cool-down
   per knob, never outside its range, and every change is written with its
   evidence to `paper_state/learning_overrides.yaml` (`history` keeps every
   step). With `learning.auto_apply` on (default) the session layers those
   overrides under your own settings — explicit settings always win — and
   reloads them automatically; `qmag learn --reset` (or the page's *Reset*
   button) drops them all.
6. **Scorecards and auto-revert.** Every adjustment in force is judged
   afterwards by what it did, not by the evidence that motivated it: a
   tightening by the resolved shadows it has kept out since (the band
   between the old and new value), a loosening by the real trades it
   admitted. Each gets a verdict — *pending* until `min_trades // 2`
   outcomes exist, then *helping*, *neutral* or *hurting*. A **hurting**
   adjustment is **reverted** at the next review (recorded in `history` as a
   `revert`) and the knob rests 28 days before it may move again. The
   learning page shows the scorecard next to each learned value.

The report lives in `paper_state/learning_report.json`: the **🧠 learning**
page shows the status (`insufficient` until `min_trades` closed trades
exist), the objective (total R, R per week, trades per week and what the
filters left on the table), the journal summary, lessons, each knob as a
bar between its guard rails with the base and learned value and its
scorecard, the adjustment history, the best missed / worst dodged shadows,
the recent post-mortems and the shadow ledger. The CLI prints the same
(`qmag learn [--no-apply] [--no-ai] [--json]`). The review reads only what
the system itself recorded — no back-filled or synthetic outcomes.

Each task is wrapped so an exception is logged and the loop continues; a
heartbeat (with the process id) is written to `paper_state/daemon_status.json`
(shown in the dashboard). The daemon holds a **single-instance lock**
(`paper_state/daemon.lock`): a second `qmag daemon` on the same state
directory — which would place every order twice — exits with an error naming
the running process, while a lock left behind by a crash is detected as
stale and taken over. `SIGTERM` / Ctrl-C stops the loop within a second and
releases the lock. Run it under a supervisor so a crash or reboot restarts it:

```bash
# systemd (see deploy/qmag.service)
sudo cp deploy/qmag.service /etc/systemd/system/ && sudo systemctl enable --now qmag

# Docker
docker build -t qmag . && docker run -d --restart unless-stopped \
  -v $PWD/paper_state:/app/paper_state -v $PWD/data:/app/data \
  -e ALPACA_API_KEY -e ALPACA_SECRET_KEY -p 8765:8765 qmag
```

The Docker image runs the daemon and the dashboard together (`deploy/start.sh`).
For IBKR you also need a running IB Gateway (e.g. the `ghcr.io/gnzsnz/ib-gateway`
image) and `IBKR_HOST` pointing at it.

### Dashboard

`qmag dashboard` serves the desk over the state directory. Every page shares
the same header: the desk's name, a primary navigation bar (Desk · Accounts ·
Insider scan · Advisor · Learning · Settings · Status, the current page
underlined), a ticker lookup, and a status strip of pills — paper or **LIVE**
broker, connection health, options flow, edge score, reviewer mode, then the
pills and buttons specific to the page. A thrown kill switch or a data gap
adds a red banner on every page.

* **Desk (`/`)** — regime and equity, the effective risk per trade, which
  context sources answered this cycle, the theme leaderboard, then every
  trade plan as a card: annotated chart, entry / stop / target / shares, the
  verdict, sentiment gauges (news, social, options flow), earnings distance,
  float and short interest, the full pass/fail checklist and an expandable
  *Why this trade* with the entry, sizing, stop and profit-taking reasoning.
  In a risk-off tape the setups that triggered are still shown as **Blocked
  setups** with exactly which check stopped them. Below: open positions with
  their stage, pending buy-stops, watchlist, rejected ideas, the trade
  journal (win rate, average and total R, realised P&L, the last ten trades as
  R bars, the current risk multiplier), closed trades and the cycle log.
* **Plan detail (`/plan/SYMBOL`)** — the full written justification, bull
  vs bear case, verdict (and the LLM committee's, if enabled), every headline
  with its score and catalyst tags, social samples and counts, options flow,
  fundamentals and events.
* **Lookup (`/symbol/SYMBOL`)** — type any ticker into the header: the data
  is refreshed, the detectors run, a plan is sized against the current
  account, context is gathered and the chart is drawn — a desk check for a
  name you are curious about, without trading or saving anything. When
  there *is* a setup, an **Act on this** box offers what the method allows:
  **Buy** (above the pivot, every check and confirmation gate passing) sends
  a market order for the plan's shares with the stop and target attached and
  hands the position to the trader; **Override and buy** does the same when
  a check or gate failed and journals the trade as an override so the review
  can score your calls against the rules; **Arm** puts a flag that has not
  broken out yet on the focused passes' list for up to five sessions
  (surviving the nightly rebuild). Buying below the pivot is never offered,
  and a plan whose stop sits at or above the market cannot be overridden.
  The plan is rebuilt from fresh data at click time — nothing is sent from
  numbers the page showed earlier — and live accounts ask for an explicit
  confirmation.
* **Learning (`/learning`, the 🧠 pill)** — the Saturday review: journal
  summary, lessons, the tunable knobs between their guard rails with base
  vs learned values, the adjustment history with its evidence, recent
  post-mortems and the shadow ledger; *Run review now* and *Reset learned
  values* buttons. The desk shows the entry mode, the names **held back**
  this pass with the gate that stopped them, and a learning summary card.
* **Accounts (`/accounts`, the 💼 pill)** — every account a qmag desk drives,
  this one and the desks listed in `QMAG_DESKS`: equity, cash, stocks at
  market, open P&L, today's change, realised P&L today / total, resting
  orders, then each account's holdings (quantity, average cost, last real
  close, market value, unrealised P&L, stop / target / setup when qmag manages
  it, and a note when the broker holds something qmag does not manage).
  Totals are summed per currency — no exchange rate is invented. Each desk's
  daemon heartbeat and kill switch are shown; another desk's switch can be
  thrown or cleared from here. *Refresh this desk from the broker* rebuilds
  the local snapshot on demand. The desk page's open-positions table also
  carries the last close and unrealised P&L from the same snapshot. See
  [Several accounts](#several-accounts-one-desk-per-account).
* **Connections (`/status`)** — the health page (see *Data integrity*
  below): every data provider, the broker, each context source, the LLMs,
  the daemon and the last cycle with its state, when it last answered,
  latency, item counts and the last error. `Test connections now` exercises
  everything that is enabled and records the result. **Broker order
  routing** sends real test orders through the broker: a **bracket test**
  places a 1-share buy-stop bracket far above the market, confirms it is
  resting, cancels it and confirms it is gone (safe on live accounts, never
  fills); a **fill test** buys one share at market, checks the position,
  sells it and checks the account is flat again (paper only from the page;
  `qmag broker-test --kind fill --allow-live` from the CLI inside market
  hours). Every step is listed with its latency, the last runs are kept in
  `order_tests.json`, and the outcome feeds the `broker_orders` connection.
  The **Kill switch** section halts all new entries — buy-stops already
  resting at the broker are cancelled the moment it is thrown, so nothing
  can fire before the next cycle — with a reason that is shown in a red pill
  and banner on every page, optionally flattens every open position at
  market, and resumes trading; **Alerts** sends a test
  push; the **Unusual Whales budget** card shows today's call count, the cap
  and whether the source is paused.
* **Advisor (`/advisor`)** — say what you want in plain English (*"risk
  half as much per trade until I have twenty closed trades"*, *"be stricter
  about entries"*, *"explain my current risk settings"*) and the model of your
  choice answers with advice and, when you asked for a change, a list of exact
  setting changes: key, current value, proposed value and why. **Nothing
  changes until you press Apply**: every proposal is validated the same way
  as the settings page (type, range, cross-field rules), pinned command-line
  overrides and the advisor's own settings are refused, and API keys, the
  broker and the live switch are out of its reach entirely. Accepted changes
  are written to `settings.yaml` in one validated save; the daemon picks them
  up at its next cycle. The model sees the desk's state (equity, positions,
  recent R, regime, kill switch, learned adjustments, lessons) and a map of
  every strategy setting with its meaning, current value, default and allowed
  range — and nothing else. Conversation and pending proposals live in
  `advisor.json`; applied changes are recorded as `advisor_apply` on the status
  page. The same from a terminal: `qmag advise "…"` (add `--apply` to accept
  every valid proposal in one go).
* **Settings (`/settings`)** — see the next section: every strategy
  parameter, the data source, brokers and API keys, the language model
  behind each AI feature, with download / upload of the whole configuration.

`Run cycle now` is available for paper brokers. JSON at `/api/snapshot`,
`/api/report`, `/api/symbol/SYMBOL` (and `POST /api/symbol/SYMBOL/trade`
with `{"action": "arm" | "buy" | "override", "confirm_live": bool}`; refusals
come back as `409` with the reason and which alternative applies),
`/api/status` (and `POST /api/status/probe` — all connections in the
background, or `{"names": [...]}` for a synchronous test of just those —
`POST /api/status/order-test`,
`/api/status/order-tests`), `GET`/`POST /api/halt` (`{"on": bool, "reason":
str, "flatten": bool, "confirm_live": bool}`), `POST /api/alerts/test`,
`/api/settings` (secrets masked), `GET /api/llm/providers` (the AI providers,
keys masked) and `GET /api/llm/models?provider=<id>|all` (the models each
provider's key can actually call), `/api/advisor` (and `POST
/api/advisor/ask` `{"message": str}`, `POST /api/advisor/apply` `{"ids":
[...]}`, `POST /api/advisor/dismiss`, `POST /api/advisor/clear`). The layout
is responsive down to phone width.

### Settings page: keys, parameters, download / upload

`http://127.0.0.1:8765/settings` is where the desk is configured. Nothing on
it needs a restart: the dashboard applies a save immediately and a running
daemon (a separate process) re-reads the files at the start of its next
cycle when they changed.

* **Quick settings** — the dials that matter most on one card: risk per
  trade, position caps, stop and profit-taking rules, the regime / theme /
  context switches, the options-flow and edge-score gates with their
  threshold and coverage, the LLM reviewer mode.
* **Connections & API keys** — one card per service (price data source,
  Unusual Whales, Alpaca, Interactive Brokers, MetaTrader 5, Reddit, LLMs,
  engine). Secrets are entered in password fields and **never rendered back**
  - the page shows `••••` plus the last four characters and whether the value
  is *saved* (in the file) or inherited from the *environment*. A blank field
  keeps the saved key; a *forget the saved key* box removes it. Saving a key
  takes effect at once (e.g. saving the Unusual Whales key flips `--data auto`
  to Unusual Whales and turns the flow scan and the edge score on), and the
  [Connections page](#dashboard) is one click away to test it.
* **AI providers** — one card per language-model vendor: name, endpoint
  URL (for OpenAI-compatible ones), API key, **Save**, **Test** (lists the
  models the key can call) and **Remove**. Google Gemini and OpenAI are
  built in; *Add a provider* offers presets for Anthropic Claude, xAI Grok,
  Groq, OpenRouter, DeepSeek, Mistral, Together, Ollama and a custom
  endpoint — pick one, paste the key, and it appears in every seat's
  provider list. A local endpoint (Ollama, vLLM on your LAN) needs no key;
  hosted vendors show NO KEY until one is saved. Stored in
  `state_dir/llm_providers.json` (0600); the built-ins still read
  `GEMINI_API_KEY` / `OPENAI_API_KEY` from the environment when nothing is
  saved. `POST /settings/providers` / `/settings/providers/delete` are the
  form targets; `GET /api/llm/providers` lists them with keys masked.
* **Language models** — one card per AI seat (trade reviewer, the three
  analyst-committee seats, insider scan analyst, learning review, desk
  advisor): on/off, **provider** (auto or any provider from the list above)
  and **model**. The model field offers the models that provider's key can
  actually call, fetched live (`/api/llm/models`, refreshed on demand), so a
  retired name is never guessed at; the card says whether the chosen
  provider is usable, and its **Test** button sends one real request with
  the saved choice and shows the model's answer or the provider's error
  (with any key or token redacted). A seat whose provider was removed says
  so instead of silently switching. The status page has the same per-row
  **Test** so one connection can be retried without probing everything.
* **All strategy parameters** — every section of the strategy YAML as a
  form, with a *find a parameter* box, the default shown for anything you
  changed, and validation before anything is written (a wrong type or an
  out-of-range value is rejected and the saved file is left untouched).
* **Advanced: YAML** — the same configuration as text, in exactly the
  format `--config file.yaml` reads.
* **Download & upload** — one YAML bundle with the strategy, the
  connection settings and the AI providers (`ai_providers`). API keys are
  left out unless you tick *with API keys*
  (the file then says so at the top and must be treated like a password).
  Upload accepts such a bundle or a plain strategy YAML; the file is validated
  first, and a checkbox decides whether the keys in it are applied or your
  locally saved keys are kept.

Where it lives: `state_dir/settings.yaml` (strategy) and
`state_dir/settings.env` (connections, written with owner-only `0600`
permissions). Once `settings.yaml` exists it takes over from `--config`;
command-line flags such as `--no-themes` or `--no-options-flow` still apply on
top and the page lists them. Saved connection values are exported into the
process environment when a session starts or reloads (a saved value wins over
the same variable from the shell), so every module keeps reading
`UNUSUAL_WHALES_API_KEY` & co. exactly as before. The broker itself is chosen
on the command line (`--broker`) so a browser can never switch a running desk
from paper to live.

## Operating controls: kill switch, alerts, API budget, remote access

**Kill switch.** `qmag halt --reason "..."` (or the button on `/status`, or
`POST /api/halt`) writes `halt.json` into the state directory. Every process
that shares that directory honours it on its next pass: the daemon skips
planning and context gathering, cancels resting buy-stops, and keeps managing
open positions (stops, partials, trails still run — the switch never leaves a
position unprotected); the lookup page refuses **Buy** and **Override**
(arming is still allowed); broker order tests are refused; the report carries
`HALTED by dashboard 2026-09-12 09:41: reason` and every page shows a red
**TRADING HALTED** pill and banner. `--flatten` (or *Halt and flatten*) also
sells every open position at market and journals the closes with reason
`halt`, so the review sees them. Flattening a live account needs `--yes-live`
/ an explicit confirmation. `qmag resume` clears it. It is a file rather than
a setting so it survives restarts and can be thrown from a shell on the box
when the dashboard is unreachable.

**Alerts.** Two optional channels, configured on the settings page: a
**Telegram** bot (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) and a **webhook**
URL (`QMAG_ALERT_WEBHOOK_URL`; the JSON body carries `text` for Slack,
`content` for Discord and structured fields for anything else). What is
pushed: fills and manual entries, exits and partials with their R, the kill
switch thrown or cleared, the daily loss limit tripping, a data problem
blocking new entries, a cycle or daemon task failing, and a failed order
test. Delivery runs on a background thread so a slow endpoint never delays a
cycle, repeated conditions are sent once per 12 hours, payloads never carry
secrets, and every attempt is recorded under the `alerts` connection — which
reads NOT CONFIGURED, not OK, when no channel is set. `qmag alert-test` and
the *Send a test alert* button prove the path; `qmag alert "title" "body"`
pushes one message from any script or cron job.

**Unusual Whales daily budget.** All Unusual Whales calls from the daemon, the
dashboard and the CLI are counted per UTC day in `data/cache/uw_budget.json`.
`UNUSUAL_WHALES_DAILY_CAP` (settings page; 0 = uncapped) stops calls locally
once the cap is reached; independently, an API answer that says the plan's
daily limit is exhausted pauses calls until midnight UTC (an ordinary rate
spike 429 is still retried after `Retry-After`). While blocked, every request
returns an error that the callers record as a data gap — flow, edge and (if
selected) price readings from that source stay empty and say why; nothing is
substituted. `/status` shows the count, the cap and the pause.

**Remote access.** The dashboard binds to `127.0.0.1` by default and is meant
to be reached over an SSH tunnel. To watch and steer the desk from anywhere,
set a **dashboard password** (`QMAG_DASHBOARD_PASSWORD`, settings page →
*Remote access*, at least 8 characters) and then put an HTTPS entry point in
front of the port. With a password set every page and API call requires a
sign-in: a 7-day `HttpOnly` cookie signed with a random per-desk secret
(`<state-dir>/dashboard_secret`, so restarts keep sessions but a changed
password logs everyone out), or `Authorization: Bearer <password>` for
scripts. Five wrong attempts lock that client address out for 15 minutes; the
`/healthz` probe stays open; `qmag dashboard` prints a red warning if you bind
to a non-loopback address without a password. The recommended entry point is
a **Cloudflare Tunnel** (`deploy/install-tunnel.sh`): `cloudflared` on the
box opens an outbound connection to Cloudflare, so no inbound port or
firewall rule is needed and the dashboard stays on loopback. Without an
account you get a *quick tunnel* — a random `https://….trycloudflare.com`
hostname that changes when `cloudflared` restarts (`deploy/tunnel-url.sh`
prints the current one). Because that address can change after a reboot or
an update, the installer also sets up a `qmag-tunnel-watch` timer
(`deploy/tunnel-watch.sh`, every minute) that records the current address
in `<state-dir>/tunnel_url.txt` — shown as **Public address** on the
settings page — and pushes an alert over your Telegram / webhook channel
whenever it changes, so the new address reaches your phone without you
having to log in to the box. With a free Cloudflare account and a domain on it,
create a named tunnel in Zero Trust → Networks → Tunnels, map a hostname to
`http://127.0.0.1:8855`, and run the installer with `TUNNEL_TOKEN=…` for a
fixed address; Cloudflare Access can add a second login (email one-time code)
in front of it. Tailscale Serve/Funnel or any reverse proxy with TLS work the
same way — the only requirement is that the password is set before the port
is reachable from outside.

## Data integrity: nothing is made up

The desk only acts on data it actually received. There is no fallback
value, default score or "reasonable guess" anywhere in the path from source
to order. When something cannot be sourced it is left empty, the gap is
written down, and whatever depended on it fails its check.

What that means concretely:

| Situation | What qmag does |
| --- | --- |
| Broker account cannot be read | The cycle stops before touching state (the error is recorded). A desk lookup builds the plan with **0 shares**, fails a `broker_account` check and says *NOT SIZED* — never sizes against a configured or remembered balance. |
| Benchmark (QQQ) or VIX bars missing / stale, or too few for the MA | Regime is **UNKNOWN**, treated as risk-off; the report and every plan carry `regime unknown: …` in `data_gaps`. |
| Price feed a whole session behind (Yahoo down, cache served) | Exits are still managed, but every new plan fails `price_data_fresh`; the report says `price data stale: latest bar …, expected …`. Stale cache files are never re-stamped as fresh; the next load retries. |
| Symbols that returned no bars or whose refresh failed | Counted and reported (`missing`, `stale`) on the price-data connection; never filled in. |
| Context source down (finviz, Yahoo, StockTwits, Reddit, Unusual Whales) | The reading stays `None`, the source is listed as unavailable on the plan, in the rationale and in the LLM bundle; soft gates pass on *missing* data by design, `options_flow.require_bullish` fails. |
| An Unusual Whales edge feature does not answer (HTTP error, empty, unknown shape, not on your plan) | Its sub-score is `None`, never 0: it is dropped from the weighted average, lowers `coverage`, and is named with the API's reason on the plan, in the rationale, on `/status` and in the LLM bundle. Coverage below `edge.min_coverage`, or no score at all, fails the entry gate. |
| Whole-market sweep on `--data unusual_whales` | Unusual Whales has no bulk bars endpoint; above `UNUSUAL_WHALES_BULK_THRESHOLD` symbols the sweep is served by Yahoo batches and the price-data status says exactly that. Set the threshold to 0 to refuse the fallback. |
| LLM committee / reviewer unreachable | `{"error": …}` is stored on the plan; `reviewer.fail_closed` decides whether that blocks the trade. |
| Not enough history for ADR on a lookup | The chart is drawn with **no** illustrative stop / target instead of a guessed range. |
| `--data synthetic` (or any generated source) | Refused at every entry point: *qmag never uses simulated or generated prices*. There is no synthetic provider in the product; the only generated frames are test fixtures under `tests/`. |

Every attempt against every connection (success, latency, item count, or
the error) is recorded in `state_dir/connections.json` by whichever process
made it — daemon, CLI or dashboard — and summarised on **`/status`** and by
**`qmag status`**:

```
$ qmag status                # from the registry, no network
$ qmag status --probe        # exercise every enabled connection now
$ qmag status --json
```

States: **OK** (answered on the last attempt), **DEGRADED** (partly:
some symbols or endpoints failed, or stale), **ERROR** (last attempt
failed), **NOT CONFIGURED** (enabled but missing a key / file / terminal),
**NOT CHECKED YET** (enabled, nothing has used it), **OFF** (disabled in
config). Connections marked *required* (price data, universe,
regime inputs, broker) block new positions while they are down; the header
pill on every page turns red when one is.

## What is (and is not) automatable

Kullamägi's edge, by his own account, is mostly *discretionary*: reading the
character of a consolidation, judging the catalyst behind a gap, choosing
which 10 of 40 valid breakouts to take, and sizing up aggressively when the
market is paying. The parts that translate cleanly to code are implemented
here: the momentum screen, the geometric flag definition, the EP filter, the
sizing, and the exit ladder. The parts that do not:

* **Intraday entries.** He buys the opening-range-high break on 1/5/60-minute
  bars and stops at the low of day. Daily bars approximate this with
  `pivot + buffer` and `entry − 1 ADR`. Plugging in intraday data (Alpaca,
  Polygon) is the single biggest fidelity upgrade.
* **Catalyst quality.** EPs on real earnings beats behave differently from
  gaps on rumours. The context layer tags the catalyst (earnings, guidance,
  FDA, contract, offering…) and shows it on every plan, but it does not yet
  *gate* EPs on catalyst type — that decision is left to you until there is
  enough journal data to test it.
* **Market feel.** QQQ trend plus universe breadth (and optionally VIX) is
  still a stand-in for "the market is paying breakouts right now". The
  adaptive-risk loop over realised R is the closest mechanical analogue.
* **Full-day volume** is used to confirm a breakout that you would, in
  practice, buy before the volume is in. Live runs after the open use the
  partial bar, which is honest; backtests are slightly optimistic here.

Survivorship bias is the other big caveat: any static ticker list you
backtest today is made of winners. Use a point-in-time universe if you want
numbers you can trust.

## Layout

```
src/qmag/
  config.py        every parameter, YAML-loadable, dotted overrides
  indicators.py    ADR, momentum returns, relative volume, SMAs
  uw.py            Unusual Whales client: bearer auth, process-wide throttle, TTL disk cache, 429 retry, shared daily call budget / daily-limit pause
  data.py          Unusual Whales / yfinance / IBKR / MetaTrader 5 / CSV providers on incremental caches (no simulated source)
  themes.py        theme indices, percentile ranks, theme breadth (curated + industry auto-themes)
  fundamentals.py  finviz fundamentals crawl -> universe/fundamentals.csv, industry themes
  regime.py        market-sentiment gates: benchmark trend, breadth, VIX
  sentiment.py     pluggable per-symbol historical sentiment (CSV provider bundled)
  context/         live context: sources.py (finviz, Yahoo, StockTwits, Reddit, Unusual Whales flow),
                   edge.py (22 Unusual Whales features -> weighted, thresholded edge score with coverage),
                   scoring.py (VADER + finance lexicon, optional FinBERT), gather.py (parallel + cache)
  setups/          breakout.py, episodic_pivot.py (+ Signal type)
  backtest.py      portfolio simulator + metrics
  optimize.py      walk-forward grid search
  plan.py          TradePlan: sizing, levels, checklist, context gates, adaptive risk
  rationale.py     written justification: setup, entry, size, stop, profit plan, bull/bear, verdict
  llm.py           optional bull/bear/risk committee over any OpenAI-compatible API
  reviewer.py      additive LLM reviewer: edge bundle -> strict-JSON BUY/SELL/HOLD verdict (Gemini native or OpenAI-compatible)
  charts.py        annotated setup charts (mplfinance)
  broker.py        PaperBroker (JSON ledger), AlpacaBroker, IBKRBroker, MT5Broker
  trader.py        one cycle: reconcile → manage (incl. time stop) → gather context → check/size/justify → portfolio gates (heat, daily loss, theme) → confirm/place; entry modes; trade journal with features, MFE/MAE, post-mortems; shadow ledger
  halt.py          the kill switch file (halt.json) every process honours
  accounts.py      per-desk account.json (broker equity / cash, holdings marked at the last real bar, P&L); QMAG_DESKS overview and cross-desk halt
  alerts.py        push alerts: Telegram bot + webhook, background delivery, de-duplication, recorded under the alerts connection
  learning.py      the review: total-R objective, bucket stats, lessons, shadow + near-miss follow-up, bounded knob adjustments with scorecards / auto-revert -> learning_overrides.yaml, optional AI post-mortems
  session.py       data + config + broker + state dir; writes last_report.json + charts; symbol lookup; manual arm / buy / override; halt / flatten / resume; reloads saved settings + learned overrides
  broker_test.py   test orders through the live broker: bracket round trip, paper fill-and-flatten -> order_tests.json + the broker_orders connection
  settings.py      settings store: settings.yaml + settings.env (0600), masking, form <-> config, export / import bundle
  providers.py     AI providers: presets (Gemini, OpenAI, Anthropic, xAI, Groq, OpenRouter, DeepSeek, Mistral, Together, Ollama, custom), llm_providers.json (0600), env fallbacks, per-seat resolution
  daemon.py        the 24/7 scheduler (tiered timetable from the live config), single-instance lock
  market_calendar.py  NYSE holidays, early closes, trading days
  pace.py          time-of-day volume projection for partial intraday bars
  screener.py      pre-market gap / intraday movers screens (Unusual Whales, finviz fallback)
  insider_scan.py  Saturday unusual-options review + AI catalyst analysis
  health.py        connection registry (connections.json), per-connection status, active probes, price freshness
  dashboard.py     FastAPI dashboard (templates/: base, index, detail, status, settings, accounts, insider, learning, macros)
  universe.py      whole-market listing fetch + liquidity filter
  cli.py           typer CLI
universe/market.txt        built whole-market universe (regenerate with `qmag universe build`)
universe/fundamentals.csv  sector / industry / float / short interest / earnings per universe name
universe/default.txt       small starter list
universe/themes.yaml       hand-kept theme -> tickers groups
deploy/                Dockerfile helpers, systemd units, VM installer / SSH push, IB Gateway installer, Cloudflare tunnel, add-desk.sh (one more account)
tests/
```

## Roadmap ideas

* Parabolic short setup (his third setup) — needs a broker with shorting.
* Intraday data provider + true ORH entries and LOD stops.
* Point-in-time universe (delisted names) for unbiased backtests.
* Per-setup position limits; "recent breakout hit rate" as a regime input
  (the journal already records what is needed).
* Catalyst-type gating for EPs once the journal has enough tagged trades.
* Point-in-time news/social archives so the context gates can be backtested.
* Intraday bars for the focused passes (today's bar is a partial daily bar; volume is projected to pace, price levels are not).
