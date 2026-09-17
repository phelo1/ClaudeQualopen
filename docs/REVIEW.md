# Claude_Qual — whole-system review

Reviewed source: `phelo1/Claude_Qual`, commit `eae50d69c3aeee7d8c376773950eb56267258541`. Destination: `phelo1/ClaudeQualopen`. Review date: 16 September 2026.

## Verdict

This is a substantial, useful research and paper-trading application, with a coherent momentum strategy and unusually good explanatory intent. It is not yet a demonstrated profitable strategy, a trained model, or a reliable unattended live execution platform. The main problem is not lack of features. It is that several documented guarantees are stronger than the implementation, and the measurement system used to improve the strategy is not sufficiently separated from simulation and estimation.

Preserve the working capabilities and compatibility. Rebuild the operating experience and the boundaries around execution, evidence, and persistence. Adding another model or another indicator before fixing those boundaries would make the system harder to evaluate.

This review covers the complete repository inventory: application modules, templates, configuration, tests, deployment scripts, documentation, and bundled universe files. Critical execution and learning paths received detailed control-flow review. Offline tests and targeted reproductions supplement static inspection. It does not certify external broker behavior, paid feeds, production performance, or profitability; no account credentials or historical operating state were supplied.

## 1. What the program is trying to do

The program translates a discretionary momentum swing-trading method into daily-bar rules. It scans a US equity universe for two setups: leaders consolidating before a breakout, and neglected stocks making a large episodic gap on volume. Market trend, breadth, themes, liquidity, context, and portfolio limits determine whether a candidate becomes an order. It then manages stops, partial exits, breakeven, moving-average trails, failed breakouts, and time stops.

The architecture is a Python package, `qmag`, with a Typer CLI, FastAPI/Jinja dashboard, scheduler, data adapters, broker adapters, and JSON/YAML/CSV state. A tiered schedule reduces expensive whole-market work to nightly/post-open scans and uses a smaller arming list intraday. Alpaca, IBKR, MT5, and a local paper ledger share a broker interface. News/social/options sources and language models add context. A weekly review proposes or applies changes to eight bounded strategy knobs.

The product has three different jobs that should be visibly distinct:

1. **Research:** formulate and evaluate hypotheses using historical data and counterfactuals.
2. **Operations:** show what is known, what is missing, what is held, and what has actually been sent to a broker.
3. **Execution:** apply deterministic risk limits and reconcile orders with external reality.

Currently those jobs share mutable state and use some of the same words for materially different things. For example, an armed plan is shown in a section described as “orders placed,” and estimated exit proceeds feed the same journal used for learning.

## 2. Strategy and implementation

### What is sound

- The setup detectors, sizing, trade plan, and written rationale are separate concepts.
- Config uses frozen dataclasses; UI forms expose defaults and help text.
- The tiered scan, paid-source caps, caches, and volume projection show attention to cost and timing.
- Unknown source readings are often represented as missing values and explained in data gaps.
- Paper trading is the default, with several explicit live-account controls.
- The code retains rejected and held candidates, which is necessary to study selection bias.
- Tests use synthetic fixtures rather than requiring a brokerage account.

### Where the method becomes an approximation

Daily high/low bars do not reveal the ordering of intraday events. A daily high crossing a pivot and full-day volume exceeding a threshold do not establish that the volume was known when the pivot crossed. The backtester buys at a pivot/open using same-day volume and, for episodic pivots, same-day close-dependent checks. It is therefore a research approximation rather than a realistic replay of the confirmed-entry engine.

The volume pace curve is a heuristic shared across names and compressed for early closes. It should be tested by liquidity class and time of day. Current universe membership and industry classifications are not point-in-time historical inputs. Forward-filling theme prices makes absent observations look stationary. These are separate sources of bias; a walk-forward split alone cannot remove them.

The options “edge” is a manually weighted score, not a calibrated probability or independently established trading edge. Several inputs describe related options activity, so the blend can count correlated evidence more than once. Coverage helps identify missing data but does not quantify prediction uncertainty. News tone is not the same as catalyst quality. The unusual-options scan appropriately produces leads for investigation; “insider” terminology and suspicion percentages should not imply proof of insider activity.

## 3. Training, incentives, and improvement

### What training actually means here

There is no fitted machine-learning model, gradient update, reinforcement-learning policy, training dataset split, or reward-model optimization. Language models are called for opinions and explanations; their weights do not change. The adaptive mechanisms are:

- A recent-realized-R multiplier changes position risk when the book is hot or cold.
- Feature buckets compare trade outcomes with the overall book.
- Shadow trades simulate what rejected or held candidates might have done.
- Near-miss scans loosen detector settings and record additional hypothetical candidates.
- A weekly review moves bounded configuration values and later scores/reverts them.
- A separate walk-forward optimizer searches historical parameter combinations.

### The intended incentive

The learning objective is total R per week rather than average R alone. That is a useful response to over-filtering: deleting positive but below-average trades can improve the average while reducing total gains. Tightening is meant to remove losing marginal trades; loosening is meant to admit profitable blocked trades.

However, total R is not capital-weighted return. A tiny trade and a large trade contribute equally in R units. Summing hypothetical rejected trades ignores competing positions, correlated exposures, unavailable buying power, overlapping candidates, execution costs, and timing. “Left on the table” is not realizable opportunity P&L. The shadow model closes the whole hypothetical trade at the real strategy's partial target, omits the real exit ladder, and excludes the signal day's path for immediate shadows. It is not comparable to live realized R.

### Why the feedback can move the wrong way

- **Estimated outcomes:** broker-side exits are inferred from daily OHLC; even a market sell can be journaled before its fill is confirmed.
- **Tiny samples and many comparisons:** default eight trades, four marginal examples, multiple knobs and feature buckets; shrinkage is not a statistical confidence test.
- **Evidence reuse:** after cooldown, old outcomes can support another change without new evidence.
- **Wrong marginal evidence:** loosening uses all shadows blocked by a knob, not just examples a one-step change would admit.
- **Attribution mismatch:** breakout-only knobs can be influenced by unrelated setups; projected intraday volume and completed daily volume are different quantities.
- **Simultaneous changes:** several knobs can change together, undermining attribution of subsequent performance.
- **No true holdout promotion:** scorecards are observational before/after comparisons, not independent champion/challenger experiments.
- **Defaults:** automatic application is on, although the evidence is exploratory.

### Better improvement contract

Record provenance on every outcome. Keep local paper fills, verified broker fills, estimated journal records, and hypothetical shadows visibly separate. Use verified evidence for automatic decisions; leave uncertain records inspectable. Default to proposals, not automatic strategy changes. Require fresh, attributable, finite evidence in the actual marginal band, preserve exact step sizes, and enforce the allowlist on loaded overrides. Report uncertainty and sample size alongside estimates. Move one knob at a time when automatic application is explicitly enabled.

For a future promotion protocol, freeze a candidate configuration and compare it with the incumbent on untouched chronological data and then forward paper execution, using equal starting capital, costs, portfolio limits, and a point-in-time universe. Primary outcome: net portfolio return subject to drawdown/heat constraints. Supporting measures: expectancy, total R, turnover, exposure, tail losses, missed opportunities, data coverage, and model/API cost. Promotion should depend on prespecified evidence, not a persuasive AI narrative. This protocol is a research requirement, not a claim that sufficient evidence currently exists.

## 4. Priority findings

References below point to the reviewed source snapshot. Severity describes impact when the triggering path is used.

| ID | Priority | Finding and consequence | Source |
|---|---|---|---|
| F01 | Critical | Accepted sell requests are treated as completed exits. A delayed/rejected market sell removes management state; flatten can substitute entry price for an unknown fill. The position may remain while the book says it closed. | `trader.py:808–868`, `session.py:990–1005` |
| F02 | High | No shared transaction lock covers dashboard, CLI, and daemon read/modify/write operations. Different dashboard locks also permit a cycle and manual trade together. PaperBroker keeps a process-local ledger. Lost updates and duplicate orders are possible. | `dashboard.py:253,697,802`, `session.py:316`, `trader.py:251`, `broker.py:121` |
| F03 | High | Triggered and watch plans are sized before orders consume cash/exposure. Updating local cash afterward never changes the already-created sizes. Multiple candidates can overspend available cash or exceed gross exposure. Pending commitments also need reservation. | `trader.py:1034–1138`, `plan.py:90` |
| F04 | High | `context_checks` returns no checks when context is absent. A gather failure or candidate beyond the context cap can bypass required bullish-flow/edge evidence entirely. | `plan.py:140–148` |
| F05 | High | Broker-reconciled P&L is estimated from bars and feeds adaptive risk and learning without a provenance distinction. | `trader.py:720–759`, `learning.py:604` |
| F06 | High | Kill-switch persistence occurs after cancellation/flatten calls. A broker exception can prevent the halt file being written. An in-flight cycle also uses an old halt snapshot. | `session.py:969–1008`, `trader.py:654` |
| F07 | High | Daily loss protection is recomputed from current equity; no latch keeps it active after a rebound. Existing pending orders can remain. | `trader.py:897–904`, `portfolio_gate` |
| F08 | High | Research regime gates disappear when required inputs are absent; forward-filled benchmark/VIX readings can pass stale data. `market_breadth` fails on an auxiliary-only universe. | `regime.py:56–100`, `backtest.py:210–219` |
| F09 | High | Step precision is computed with floor(log10(step)); a 0.05 step is rounded to one decimal. A supposedly bounded change can move by 0.1 or not move. | `learning.py:97–100` |
| F10 | High | Loaded learning overrides are not checked against KNOBS or their bounds, so a malformed/stale override can escape the documented rails. | `learning.py:552–566`, `session.py:190` |
| F11 | High | Automatic learning uses simplified shadows, all historical evidence, small samples, and potentially several changes at once. No holdout validates that those changes improve execution. | `learning.py:401–489,997–1068`, `config.py:552–555` |
| F12 | High | Backtesting diverges from live behavior: failed-breakout exit absent, several portfolio controls absent, signal-day lookahead, commission-inclusive affordability not enforced. | `backtest.py:272–367` |
| F13 | Medium | Walk-forward falls back to the first grid combination when none qualifies and divides OOS equity by its first observed value, erasing the first day's gain/loss. Invalid fold/grid inputs lack explicit validation. | `optimize.py:118–180` |
| F14 | High | Dashboard trusts arbitrary forwarded client-IP headers for rate-limit identity. Public binding without a password only warns; there is no same-origin mutation check. | `dashboard.py:199–223`, `cli.py:943–948` |
| F15 | Medium | Numeric validation is incomplete: infinity/NaN can enter prices, configuration, model confidence, and learning statistics. | `indicators.py:15`, `settings.py:933`, `reviewer.py:434`, `learning.py:170` |
| F16 | Medium | Shared health/cache/budget writers use fixed `.tmp` names or non-atomic writes. The daily-budget check and increment are separate, so concurrent requests can overrun the cap. | `health.py:158`, `uw.py:149–179,291`, `context/base.py:133` |
| F17 | Medium | UI equates plans with orders; presence of a heartbeat file can look like a running daemon; learning copy says nothing is simulated despite shadow simulations. | `templates/index.html:21,114`, `templates/learning.html:64–74` |
| F18 | Medium | Package dependencies are open-ended; dev extra names `httpx2` while tests import `httpx`. No CI workflow or lockfile is present. Container starts two processes without robust supervision and binds publicly by default. | `pyproject.toml`, `Dockerfile`, `deploy/start.sh` |

Other limitations to retain explicitly: native broker order ownership/cancel scoping, partial fills and cancel/replace races, idempotent recovery across process crashes, delayed quote semantics, exchange calendar exceptional closures, stale theme membership, point-in-time news, LLM cost attribution, and public-provider schema drift. These require integration tests and operational evidence; unit tests alone do not settle them.

## 5. UI and information architecture

The existing dark theme is not inherently the problem. The problem is weak hierarchy: many equally weighted colored pills, numerous nested panels, emoji navigation, tiny labels, huge settings forms, and long mixed-purpose pages. Important actions and evidence freshness are hard to find. Tables overflow; extensive hover tooltips carry information that should sometimes be visible. Full-page polling can interrupt reading and lose the operator's place. Empty states tell users commands but do not show a clear setup sequence.

Rebuild around a calm, light workspace with a persistent navigation rail, a compact mode/status bar, consistent spacing, stronger typography, and restrained semantic color. Keep dense numerical tables where they help comparison. Use symbols and text together; do not rely on red/green alone. Provide a dark preference, responsive mobile navigation, keyboard focus, skip links, reduced-motion support, and useful empty/error states.

Proposed pages:

- **Overview:** mode, snapshot age, four essential metrics, attention queue, candidate pipeline, current positions, recent actions.
- **Trade desk:** full plans, charts, checklists, pending orders, held and rejected setups, arming list.
- **Accounts:** existing multi-account detail with currency separation.
- **Journal:** searchable actual outcomes, provenance, cumulative recorded R, explicit exclusions.
- **Learning:** proposals, evidence quality, sample sizes, uncertainty, active overrides, scorecards, separate hypothetical results.
- **Flow research:** unusual-options investigation, labeled as research rather than proof.
- **Advisor:** bounded proposals with explicit acceptance, retained.
- **Connections and Settings:** operational health first; configuration grouped with section links and search.

A chart or P&L number must be backed by supplied state. A new installation should show a well-designed empty workspace, never fabricated production trades or a fake equity curve.

## 6. Architecture and delivery plan

Keep the Python engine and its existing adapters. A wholesale rewrite would throw away strategy behavior and test coverage while recreating broker risk. Build the improved version in the separate repository, preserving the original source unchanged.

Introduce small focused modules for atomic persistence and shared locks, evidence/provenance, and dashboard presentation. Harden the deterministic gates and asynchronous-exit behavior. Preserve state compatibility by adding optional fields rather than destructively migrating user files. Keep the CLI command name and routes available; a new `/desk` can retain the original detailed desk while `/` becomes the overview.

Add regression tests for each repaired invariant, run the full inherited suite, exercise browser layouts and real navigation at desktop/mobile sizes, and build the distribution. Add CI so the repository verifies itself. Deliver a concise README with operating instructions and migration notes; retain the original long manual as historical documentation with an explicit warning where behavior changed.

The rebuilt release should claim stronger correctness and clearer evidence, not better financial returns. A live rollout still needs broker sandbox checks with partial/delayed/rejected fills, reconnects, unknown holdings, and restart recovery. No live orders or paid model calls are part of this review or local verification.

## 7. Subsystem coverage

| Area | Files examined | Assessment |
|---|---|---|
| Product/docs | README, AGENTS, HOW_IT_WORKS | Clear strategy intent; repeated operational material and some invariants contradict code. |
| Rules/config | config, indicators, setups/base, breakout, episodic_pivot, plan | Useful decomposition; strengthen finite validation, required checks, and allocation at submission. |
| Research | backtest, optimize | Useful approximation; surface limitations and correct missing-input/fold behavior. |
| Feedback | learning, trader features/post-mortems | Explainable but observational; fix timestamp handling, provenance, sample attribution and defaults. |
| Execution | broker, trader, session, broker_test | Keep adapters; fix submission-versus-fill semantics, locking, halt order, cash reservation. |
| Scheduling | daemon, market_calendar, pace | Tiered design good; shared writer locking, exceptional calendar and quote timing need attention. |
| Market data | data, universe, fundamentals, bundled universe files | Real feeds/caches; current snapshots introduce survivorship bias and external schema dependencies. |
| Context | context/base, sources, scoring, gather, edge; themes, sentiment, screener, uw | Coverage is useful; scoring is heuristic, missing context bypass is actionable, costs/races need control. |
| AI/research | llm, reviewer, providers, insider_scan, advisor | Advisory layers; finite numeric validation, calibrated language, explicit evidence boundaries needed. |
| Operations | accounts, health, halt, alerts, auth, redact | Good operator tools; estimated realized totals, concurrent writes and auth hardening need work. |
| Interface | dashboard, charts, all 11 HTML templates | Retain detail and tooltips, rebuild shared design and primary information hierarchy. |
| Packaging | pyproject, Dockerfile, .gitignore, deployment scripts | Add reproducibility/CI, align dev dependencies, fix container supervision, document state backups. |
| Verification | all 26 Python test/support files | Broad offline fixtures; gaps around concurrency, delayed exits, cumulative allocation and learning attribution. |

The per-file inventory and final test results are delivered with the rebuilt repository.

## 8. Delivered rebuild

The separate ClaudeQualopen rebuild implements the execution, evidence and UI changes documented in [VERIFICATION.md](VERIFICATION.md). The full rebuilt local suite passed 273 tests on 17 September 2026. See that implementation matrix for resolved findings, partial mitigations and remaining external integration work. The findings above describe the reviewed source commit, not the updated implementation.
