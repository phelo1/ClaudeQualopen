# Notes for coding agents

Read this first, then the
[README](README.md), [architecture](docs/ARCHITECTURE.md) and
[learning policy](docs/LEARNING.md).
The README is the source of truth for how the system works; this file is the
short list of things that bite.

## What this is

`qmag` is a Python 3.11+ package (`src/qmag`) that automates a
Qullamaggie-style momentum swing-trading method: whole-market scan, breakout /
episodic-pivot setups, regime / theme / news / social / options-flow context,
sized plans with written justification, charts, paper or live execution on
Alpaca / IBKR / MT5, a tiered 24/7 daemon and a FastAPI dashboard. There is no
database; every piece of state is a JSON / CSV / YAML file in the state
directory (default `paper_state/`, gitignored).

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # add ,alpaca / ,ibkr / ,mt5 / ,finbert as needed
qmag universe build                     # once; the Sunday job rebuilds it
qmag dashboard --state-dir paper_state --port 8765
qmag autopilot --state-dir paper_state  # scheduler + dashboard supervisor; paper by default
```

API keys go in either the environment or the dashboard's settings page, which
writes them to `<state-dir>/settings.env` (0600, gitignored). There is no
`.env` in the repo and there must never be one.

## Test

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider    # ~2-3 min, ~200 tests, no network
```

Every test runs offline against the synthetic provider (`tests/synthetic.py`);
reuse `tests/conftest.py` fixtures (`make_breakout_frame`, `CsvUniverse`).
Run the whole suite before committing; several tests are cross-cutting: every
`StrategyConfig` field must have `FIELD_HELP` text, every template must render
with tooltips, every exit reason must be handled by both the backtest and the
trader.

## Invariants — do not break these

1. **Never fabricate data.** If a feed, key or broker is missing, the code
   records a gap (`health.py`, `describe_connections()`), the plan says what was
   not checked, and the setup is skipped or marked — it is never scored with a
   placeholder value. Do not add defaults that pretend a check passed.
2. **Configuration is a frozen dataclass.** `StrategyConfig` in `config.py`;
   change it with `with_overrides({"group.field": value})`. Adding a field means
   a default in the dataclass and an entry in `FIELD_HELP` (`settings.py`); the
   settings page renders every field automatically and a test fails if the help
   text is missing.
3. **Secrets live in `settings.env` and `llm_providers.json` only.**
   `EnvField(..., secret=True)` in `settings.py` masks connection keys in the
   UI and the API; AI provider keys go through `providers.py` (`Provider.public()`
   masks, `redact.secret_values()` scrubs them from errors). Never log or echo a key.
4. **Live trading is opt-in at three levels**: `--broker`, `--yes-live` /
   `QMAG_YES_LIVE=1` (`cli._confirm_live`), and flattening or trading a live
   account from the dashboard needs `confirm_live` in the request body. Keep
   it that way.
5. **The kill switch wins.** `halt.json` in the state dir (`halt.py`) stops all
   new entries in the daemon, dashboard and CLI. `trader.run_cycle(halted=...)`
   and `TradingSession.manual_trade` already honour it; route any new entry
   path through them.
6. **One writer per state dir.** `persistence.serialized` coordinates mutating
   sessions across threads and local processes. Keep broker mutations inside
   this lease and refresh cached state at entry. Do not share writable state
   across hosts or spawn a second daemon on one state directory.
7. **Backtest and trader share exit rules.** `backtest.py` mirrors the
   management logic in `trader.py` (partials, trailing MA, breakeven, max hold,
   time stop). If you change one, change the other and add a test that both
   fire.
8. **Every dashboard route is behind the password.** `auth.py` + the
   middleware in `dashboard.py` cover all routes (including mounted static
   files) whenever `QMAG_DASHBOARD_PASSWORD` is set; the only exemptions are
   `/login`, `/logout` and `/healthz`. Do not add more.
9. **Learning is bounded.** `learning.KNOBS` defines `lo`/`hi`/`step` for each
   tunable knob; a review moves a knob at most one step, with evidence, and
   respects the cooldowns. Do not widen the rails without a walk-forward result.

## Where things are

| Area | Files |
| --- | --- |
| CLI | `cli.py` (Typer; `qmag = qmag.cli:app`) |
| Config / settings UI | `config.py`, `settings.py`, `templates/settings.html` |
| Data | `data.py` (providers, cache; `--data auto` = IBKR when the gateway answers, else Unusual Whales with a key, else Yahoo — see `resolve_data_kind`), `universe.py`, `fundamentals.py`, `uw.py` (Unusual Whales + `DailyBudget`), `market_calendar.py` |
| Scan / setups | `screener.py`, `indicators.py`, `setups/breakout.py`, `setups/episodic_pivot.py`, `regime.py`, `themes.py`, `sentiment.py` |
| Context / edge | `context/` (`sources.py`, `gather.py`, `scoring.py`, `edge.py`), `reviewer.py` (transports + `resolve()`), `providers.py` (AI provider registry, `llm_providers.json`), `llm.py`, `insider_scan.py` |
| Plan / rationale | `plan.py`, `rationale.py`, `charts.py` |
| Execution | `trader.py` (`run_cycle` steps 0–6, `portfolio_gate`, `ManagedPosition`), `session.py` (`TradingSession`), `broker.py`, `pace.py` |
| Ops | `daemon.py`, `halt.py`, `alerts.py`, `auth.py`, `health.py`, `broker_test.py`, `accounts.py` (per-desk `account.json`, `QMAG_DESKS` overview — one desk per broker account, orders are never mirrored), `deploy/` (VM installer, SSH push, Cloudflare tunnel, `add-desk.sh`) |
| Learning | `learning.py` |
| Advisor / secrets | `advisor.py` (plain-English requests → validated setting proposals, applied only on accept; `advisor.json`), `redact.py` (`redact_secrets`, `describe_error` — every stored or displayed error goes through it) |
| Dashboard | `dashboard.py` (FastAPI), `templates/` (Jinja; `_macros.html` has the `info()` tooltip macro) |
| Research | `backtest.py`, `optimize.py` |
| Tests | `tests/` (`conftest.py`, `synthetic.py` are the fixtures to reuse) |

## Conventions

- Type-annotated, `from __future__ import annotations`, dataclasses over dicts
  for anything that crosses a module boundary.
- Timestamps are timezone-aware; the trading clock is `America/New_York`
  (`market_calendar.py`, `LiveClock` in `trader.py`).
- New dashboard pages extend `base.html`, import `_macros.html`, and put a
  `data-tip` / `info()` on every number a user might not understand.
- New CLI commands get a row in the README table at the top and, if they touch
  orders, respect `--yes-live`.
- Scratch files go under `/tmp`, never in the repo. Commit one logical change
  at a time.


## Autonomous controller

Read [AUTOPILOT.md](docs/AUTOPILOT.md) before maintaining a running desk. Prefer structured status and scoped operations. Preserve order intents, execution IDs and fee provenance. The trained outcome model is supervised; do not describe it as RL. Changes to learner actions require prospective baseline/challenger testing. Risk limits, account identity, credentials and halt state are outside the learner's search space.
