# Review coverage

Source commit: `eae50d69c3aeee7d8c376773950eb56267258541`.

All 106 source files were inventoried. The review combines static inspection, critical-path control-flow review and offline tests; this is not a line-by-line proof of every adapter or an external integration certification. Files marked preserved remain part of the rebuilt program.

| Original file | Rebuild disposition | Responsibility / review context |
|---|---|---|
| `.gitignore` | Preserved | Project guidance, packaging or documentation |
| `AGENTS.md` | Preserved | Project guidance, packaging or documentation |
| `Dockerfile` | Preserved | Project guidance, packaging or documentation |
| `README.md` | Revised | Project guidance, packaging or documentation |
| `deploy/add-desk.sh` | Preserved | Deployment and process configuration |
| `deploy/ibgateway/.env.example` | Preserved | Deployment and process configuration |
| `deploy/ibgateway/docker-compose.yml` | Preserved | Deployment and process configuration |
| `deploy/install-ibgateway.sh` | Preserved | Deployment and process configuration |
| `deploy/install-tunnel.sh` | Preserved | Deployment and process configuration |
| `deploy/install-vm.sh` | Preserved | Deployment and process configuration |
| `deploy/push-vm.sh` | Preserved | Deployment and process configuration |
| `deploy/qmag.service` | Preserved | Deployment and process configuration |
| `deploy/start.sh` | Revised | Deployment and process configuration |
| `deploy/tunnel-url.sh` | Preserved | Deployment and process configuration |
| `deploy/tunnel-watch.sh` | Preserved | Deployment and process configuration |
| `docs/HOW_IT_WORKS.md` | Preserved | Project guidance, packaging or documentation |
| `pyproject.toml` | Revised | Project guidance, packaging or documentation |
| `src/qmag/__init__.py` | Preserved | qmag - research, backtest, optimise and paper-trade Qullamaggie-style momentum setups. |
| `src/qmag/accounts.py` | Revised | Accounts: what every account the desk touches holds, in one place. |
| `src/qmag/advisor.py` | Preserved | The desk advisor: plain English in, reviewed setting changes out. |
| `src/qmag/alerts.py` | Preserved | Push alerts: the desk tells you when something happened that you would |
| `src/qmag/auth.py` | Preserved | Password protection for the dashboard. |
| `src/qmag/backtest.py` | Revised | Daily-bar, portfolio-level backtester for the Qullamaggie playbook. |
| `src/qmag/broker.py` | Revised | Broker abstraction. |
| `src/qmag/broker_test.py` | Preserved | Order-routing tests for the connections page and the CLI. |
| `src/qmag/charts.py` | Preserved | Annotated setup charts. |
| `src/qmag/cli.py` | Revised | ``qmag`` command line interface. |
| `src/qmag/config.py` | Revised | Strategy, risk and engine parameters. |
| `src/qmag/context/__init__.py` | Preserved | Live context for trade candidates: news, social chatter, options flow, |
| `src/qmag/context/base.py` | Preserved | Shared types for the context layer plus a tiny on-disk cache. |
| `src/qmag/context/edge.py` | Preserved | Unusual Whales edge score: many independent reads of one ticker's options |
| `src/qmag/context/gather.py` | Preserved | Fetch context for a batch of candidate symbols, concurrently and cached. |
| `src/qmag/context/scoring.py` | Preserved | Headline / message scoring. |
| `src/qmag/context/sources.py` | Preserved | Concrete context sources. Each ``fetch_*`` function fills part of a |
| `src/qmag/daemon.py` | Revised | 24/7 scheduler: runs the trading routine on the US market clock. |
| `src/qmag/dashboard.py` | Revised | Read-mostly web dashboard over the trader's state directory. |
| `src/qmag/data.py` | Preserved | Market data providers. |
| `src/qmag/fundamentals.py` | Preserved | Per-symbol fundamentals snapshot (finviz) and the industry themes built from it. |
| `src/qmag/halt.py` | Revised | The kill switch. |
| `src/qmag/health.py` | Revised | Connection registry and data-integrity status. |
| `src/qmag/indicators.py` | Preserved | Vectorised indicator helpers on OHLCV DataFrames. |
| `src/qmag/insider_scan.py` | Preserved | Weekly unusual-options review: flag trades that look like somebody knows something. |
| `src/qmag/learning.py` | Revised | Learning from the trade journal: what worked, what did not, and why. |
| `src/qmag/llm.py` | Preserved | Optional three-seat LLM "analyst committee" (off by default). |
| `src/qmag/market_calendar.py` | Preserved | NYSE trading calendar (holidays, early closes) in America/New_York. |
| `src/qmag/optimize.py` | Revised | Walk-forward parameter search. |
| `src/qmag/pace.py` | Preserved | Time-of-day volume pace for intraday (partial) daily bars. |
| `src/qmag/plan.py` | Revised | A ``TradePlan`` is what we actually intend to do about a ``Signal``. |
| `src/qmag/providers.py` | Preserved | AI providers: where each language-model seat sends its request. |
| `src/qmag/rationale.py` | Preserved | Plain-English justification for a trade plan. |
| `src/qmag/redact.py` | Preserved | Keep secrets out of error strings, health records and pages. |
| `src/qmag/regime.py` | Revised | Market-sentiment regime: is the market paying momentum right now? |
| `src/qmag/reviewer.py` | Preserved | Additive LLM trade reviewer (off by default). |
| `src/qmag/screener.py` | Preserved | Market-wide screens for the tiered schedule. |
| `src/qmag/sentiment.py` | Preserved | Pluggable per-symbol sentiment (social media, news, options flow, ...). |
| `src/qmag/session.py` | Revised | A ``TradingSession`` bundles data source, config, broker and state directory. |
| `src/qmag/settings.py` | Revised | Operator settings: strategy parameters and connection credentials, saved |
| `src/qmag/setups/__init__.py` | Preserved | Project guidance, packaging or documentation |
| `src/qmag/setups/base.py` | Preserved | Project guidance, packaging or documentation |
| `src/qmag/setups/breakout.py` | Preserved | Momentum breakout: a leader consolidates in a tight flag, then clears the |
| `src/qmag/setups/episodic_pivot.py` | Preserved | Episodic pivot (EP): a large gap up on huge volume, usually on earnings or |
| `src/qmag/templates/_macros.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/accounts.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/advisor.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/base.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/detail.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/index.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/insider.html` | Preserved | UI rendering and operator workflow |
| `src/qmag/templates/learning.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/login.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/settings.html` | Revised | UI rendering and operator workflow |
| `src/qmag/templates/status.html` | Revised | UI rendering and operator workflow |
| `src/qmag/themes.py` | Preserved | Theme / segment momentum. |
| `src/qmag/trader.py` | Revised | The trading routine that turns the research rules into orders. |
| `src/qmag/universe.py` | Preserved | Symbol universes. |
| `src/qmag/uw.py` | Revised | Unusual Whales API client shared by the price provider, the context layer, |
| `tests/__init__.py` | Preserved | Offline test / fixture |
| `tests/conftest.py` | Preserved | Offline test / fixture |
| `tests/synthetic.py` | Preserved | Generated price paths for the automated test suite - and only for it. |
| `tests/test_accounts.py` | Revised | Accounts: the per-desk account snapshot, the multi-desk overview, the |
| `tests/test_advisor.py` | Preserved | Desk advisor: plain English -> validated proposals -> applied only on accept. |
| `tests/test_auth.py` | Revised | Dashboard password protection: open when unset, locked down when set. |
| `tests/test_backtest.py` | Preserved | Offline test / fixture |
| `tests/test_context.py` | Preserved | Theme momentum, market-sentiment regime and pluggable sentiment. |
| `tests/test_context_layer.py` | Preserved | Context scoring, plan gates, rationale text, LLM committee plumbing and adaptive risk. |
| `tests/test_desk_actions.py` | Revised | Acting from the desk: broker order tests on the connections page, manual |
| `tests/test_edge.py` | Revised | Unusual Whales as primary data source + the weighted edge score: client, provider, |
| `tests/test_fundamentals.py` | Preserved | Offline test / fixture |
| `tests/test_ibkr_data.py` | Preserved | IBKR as the price feed: provider behaviour against a fake ``ib_async`` and |
| `tests/test_integrity.py` | Revised | No invented data: missing inputs are flagged, never substituted. |
| `tests/test_learning.py` | Revised | Entry modes (confirmed / resting / hybrid), the failed-breakout exit, the |
| `tests/test_ops.py` | Revised | Offline test / fixture |
| `tests/test_options_flow.py` | Revised | Unusual options flow (Unusual Whales): on/off switch, fetch + scoring with mocked endpoints, |
| `tests/test_plan_and_orders.py` | Revised | Offline test / fixture |
| `tests/test_providers.py` | Revised | AI providers: any number of vendors, each with its own key and endpoint, |
| `tests/test_providers_brokers.py` | Preserved | IBKR / MT5 providers and the MT5 broker, exercised against in-memory fakes. |
| `tests/test_reviewer.py` | Preserved | Additive LLM reviewer: edge bundle, transports (mocked), verdict parsing, and how the trader uses it. |
| `tests/test_risk_ops.py` | Revised | Portfolio risk gates, the kill switch, push alerts, the Unusual Whales daily |
| `tests/test_settings.py` | Revised | Settings page: save / download / upload strategy parameters and API keys. |
| `tests/test_setups.py` | Preserved | Offline test / fixture |
| `tests/test_tiered.py` | Revised | Tiered schedule: arming list, focused passes, volume pace, screens, daemon |
| `tests/test_trader.py` | Preserved | Offline test / fixture |
| `universe/default.txt` | Preserved | Universe and theme inputs |
| `universe/fundamentals.csv` | Preserved | Universe and theme inputs |
| `universe/market.txt` | Preserved | Universe and theme inputs |
| `universe/themes.yaml` | Preserved | Universe and theme inputs |
