# Verification and delivery status

Verified 17 September 2026. Source reviewed: phelo1/Claude_Qual at
eae50d69c3aeee7d8c376773950eb56267258541. The original repository was not modified.
This report supersedes the 0.2 verification report for current behavior.

## Connection recovery — 23 September 2026

Full final Windows/Python 3.12 suite: **350 passed**, six upstream deprecation warnings, 354.86 seconds. Regressions cover concurrent requests from three separate processes, options-flow routing through the shared client, temporary 429 retries, preservation of real daily limits, recovery of legacy false pauses without resetting usage, long Retry-After handling, and degraded connection visibility.

The deployed account returned a three-concurrent-request limit, but the old classifier treated the response's upgrade link as daily exhaustion. Only 1,169 of the configured 25,000 daily calls had been counted. Requests now share one network slot per budget directory across local processes. The separate options-flow request path now uses the same client, accounting and retry rules. Historical connection errors remain available but are explicitly labelled as previous errors after recovery.

Remote verification: the incorrect pause cleared automatically with all 1,169 counted calls preserved. Six simultaneous real UW info requests queued and all returned HTTP 200. Subsequent authenticated probes passed for IBKR account access, current price data, UW options flow and UW edge research. The budget remained unpaused at 1,202 calls. Reconciliation was clean, scheduler heartbeat fresh, and local/public dashboard checks returned HTTP 200. IB Gateway's start timestamp stayed unchanged through deployment; a private backup of replaced files and the prior budget was retained. No test orders were submitted.

Remaining issues were Stocktwits HTTP 403, missing Reddit credentials, and an institutional-data lookup without reported holder changes. Learning still lacks sufficient completed evidence for a model/policy promotion. These are not the repaired concurrency failure. Sharing the API key with other applications or machines remains outside the local request lease; transient external failures are still reported and retried within bounds.

## Shadow evidence integrity — 19 September 2026

Full Windows/Python 3.12 suite: **337 passed**, six upstream deprecation warnings, 244.96 seconds. Targeted shadow/autonomy checks passed before deployment. The previous broker-fix CI completed successfully across all four configurations.

The live inspection found three invalid long-shadow configurations with stops above entries. Their resulting labels were quarantined, with original results preserved in a private state backup and each record's audit fields. The strategy remained baseline with no active trained model. New shadows validate price geometry, resolution/review quarantine legacy invalid rows, and model training independently excludes them. The Learning page displays exclusions and omits invalid returns.

Remote verification: 23 valid completed shadows, 43 open, three expired and three quarantined. The refreshed model check reports 23 eligible records of the required 80; parameter research still waits for 20 fresh holdout sessions. Learning page returned HTTP 200 and showed the exclusions. No policy change or broker order was made, and IB Gateway remained running. A regression ensures model evidence refreshes during the holdout wait without rerunning backtests on consumed data.

## Oracle migration and broker recovery — 18 September 2026

- Full Windows/Python 3.12 suite with the actual IBKR SDK installed: **323 passed**, 5 upstream asyncio/Starlette deprecation warnings, 512.92 seconds. Focused broker/data/manual-action recovery suite: **31 passed**.
- Reproduced the real IBKR 2.0.1 failure by connecting on the main thread and then a worker thread. After upgrading to 2.1.0, main-thread and two concurrent worker account reads succeeded. Three authenticated dashboard broker probes and an IBKR price-data probe succeeded; no test orders were sent.
- The SDK requirement and dependency snapshot now prevent the incompatible installation that selected 2.0.1. CI installs the IBKR extra and tests its real loop driver with mocked network responses. Tests include missing/closed loops, concurrent broker/data workers, rejection of a mismatched SDK loop, and rejection of synchronous broker calls inside a running web loop.
- The runtime guard repairs absent/closed worker loops. Recurring reconciliation checks account access after saving protection/execution results. A temporary-outage regression confirms that the next successful check clears current failure status without submitting orders.
- The deployed scheduler automatically refreshed broker health at 09:35 UTC on 18 September: OK, zero consecutive failures, successful reconciliation. IB Gateway's start timestamp remained unchanged throughout deployment and repair.
- Deployment code hashes matched the local source; remote dependency checks passed. A standard wheel was built and checked for the runtime guard and updated SDK requirement.
- Migration regression coverage includes conservative FX valuation, sufficient research history, separate research client IDs, and preservation of Resend settings. Real historical research completed with no qualifying challenger; the outcome model remained collecting. Optional external-feed warnings remain visible separately from broker health.

These checks validate recovery and broker/data reads on this paper account, not live order/fill certification. See the runbook for recovery limits. Earlier verification below describes the original v0.3 release.

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
python -m pip install -e ".[dev,ibkr]"
python -m pytest tests -q
python -m pip wheel . --no-deps --wheel-dir dist
```

Windows uses platform-appropriate permission assertions; POSIX private-file
checks still require mode 0600.
