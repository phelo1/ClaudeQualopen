# Verification and delivery status

Verified 17 September 2026. Source reviewed: phelo1/Claude_Qual at
eae50d69c3aeee7d8c376773950eb56267258541. The original repository was not modified.
This report supersedes the 0.2 verification report for current behavior.

## Executed checks

| Check | Result | Scope |
|---|---|---|
| Original source suite | 247 passed, 8 failed | Initial Windows review; portability and date-sensitive expectations |
| First rebuild (0.2) | 273 passed | Published baseline; GitHub Actions passed Ubuntu/Windows on Python 3.11/3.12 |
| Autonomous rebuild (0.3) | **301 passed in 269.94 seconds** | Full suite on Windows, Python 3.12.10, four pytest workers |
| Focused execution/research/replay checks | 28 passed | Final targeted run before the full suite |
| Wheel | Built | Python modules, templates and static assets included |
| Browser | Inspected | Operations and Learning lab; populated monitor with explicitly labelled synthetic UI fixtures, filters, annotations and mobile layout |
| CI | Configured | Every push runs the full suite and wheel build on Ubuntu/Windows, Python 3.11/3.12; see Actions for release-specific results |

The full local run emitted four instances of an upstream Starlette/AnyIO
deprecation warning, one per worker. No failures or skips remained. The labelled
synthetic chart workspace was used only to verify rendering, not as trading or
performance evidence. Published preview data does not contain those fixtures.

Local execution used workspace-specific temporary and Matplotlib directories.
This Windows sandbox required a runtime-only temporary-directory mode shim; it
is not packaged or committed. Imports were explicitly pointed at the rebuild.
requirements-tested.txt records the initial Python 3.12 environment, not a
universal resolver lock.

## Evidence added in 0.3

- Execution regression cases exercise cumulative partial fills, duplicate polls,
  late fees, lost acknowledgements, protection recovery, cancellation/fill races,
  residual exits, client ownership, and IBKR/Alpaca/MT5 response parsing.
- Paper cases cover repeated daily snapshots without reusing old extrema and
  entry-plus-stop ambiguity. Historical replay checks next-bar fills, malformed
  input, unavailable future data, refusal of current LLM reviewers, and CLI warmup.
- Research cases cover purged model training, exclusion of shadow-only
  validation, reserved holdouts, fresh-data requirements, repeated-observation
  rejection, prospective promotion, configuration invalidation and rollback.
- Operations cases cover simultaneous scheduled work, credential-free backup
  content, protected mutation endpoints, structured status and chart P&L.
- Existing strategy, context, risk, settings, authentication, portfolio,
  backtest and UI tests remain in the full suite.

## Disposition of the original review

See [REVIEW.md](REVIEW.md) for the historical findings. F01/F03/F05 now have durable
order intents, cumulative execution reconciliation, partial-fill accounting and
explicit cost provenance. F02/F06/F07/F14/F15/F16 retain coordinated writes,
persistent halts/loss latches, authentication and input validation. F08/F12/F13
have conservative next-open daily execution plus a chronological intraday replay
path. F09/F10/F11 now have bounded experiments, trained outcome coefficients,
reserved historical evaluation, prospective comparison, canary deployment and
automatic rollback. F17 adds Operations, Market monitor and the updated Learning
lab. F18 adds the local process supervisor and an operating runbook.

These statements describe tested code behavior, not certification of every
external account, instrument or API failure mode.

## Remaining boundaries

No real broker account certification, live orders, paid-feed calls, model-provider
calls or historical profitability study were performed. The test suite does not
establish improved returns. Configure and validate the IBKR paper account before
enabling live execution.

Broker reconciliation is implemented for the common long-equity order lifecycle.
Statement adjustments, corporate actions, execution busts and financing are not
fully automated. Finite broker history can leave old acknowledgements unresolved;
these block new risk. Alpaca's adapter does not have complete settled fee data,
so unknown costs cannot authorize completion of a live canary. MT5 profit targets
depend on controller availability; attached stop protection is broker-side.

Daily OHLC cannot reveal intraday event ordering. Replay improves time resolution
when supplied actual intraday data; it does not reconstruct queue position,
liquidity, spread, point-in-time universes or unavailable historical context.
The current strategy scans US-listed long equities; options research supplies
stock leads, not an options execution engine or worldwide multi-asset strategy.

Learning trains supervised outcome weights and tests bounded strategy changes.
It is not reinforcement learning or LLM fine-tuning. Observational outcomes and
repeated trials can still overfit; the block bootstrap and promotion rules are
conservative heuristics, not a proof of causal improvement. Broker authentication,
host reliability, off-machine backups and deployment security remain operational
requirements. See [Architecture](ARCHITECTURE.md), [Learning](LEARNING.md) and
[Autopilot](AUTOPILOT.md) for exact contracts and recovery steps.

## Reproduce

```bash
python -m pip install -e ".[dev]"
python -m pytest tests -q
python -m pip wheel . --no-deps --wheel-dir dist
```

Windows uses platform-appropriate permission assertions; POSIX private-file
checks still require mode 0600.
