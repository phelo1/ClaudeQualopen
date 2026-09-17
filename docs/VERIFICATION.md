# Rebuild verification and delivery status

Verified 16–17 September 2026. Source reviewed: `phelo1/Claude_Qual` at
`eae50d69c3aeee7d8c376773950eb56267258541`. The original repository was not modified.

## Executed checks

| Check | Result | Scope |
|---|---|---|
| Original test suite | 247 passed, 8 failed | Windows, Python 3.12.10; five POSIX permission assertions, two POSIX `sleep` assumptions, one date-sensitive CLI expectation |
| Rebuilt full suite | **273 passed** in 356.73 seconds | All tests, four pytest workers; 18 additional regression cases including parametrization |
| Focused safety suite | 26 passed | Execution, persistence, learning and interface checks during development |
| Wheel build | Passed | `hatchling build -t wheel`; static CSS, templates and persistence module confirmed in wheel |
| Real browser | Passed inspected states | Nine main pages at desktop and mobile widths, dark theme, empty workspace and navigation; overflow corrections checked down to 320 px |
| CI configuration | Added | GitHub Actions matrix: Ubuntu/Windows, Python 3.11/3.12; remote results are shown in the repository Actions tab |

The full suite emitted four instances of the same upstream Starlette/AnyIO
deprecation warning, one per worker. No test failures or skipped tests remained.
Offline fixtures explicitly clear inherited paid-feed credentials to avoid
order-dependent environment leakage. Existing tests were updated for the new
overview/desk routes and portable permission/process behavior.

Local execution used workspace-specific temporary and Matplotlib directories.
This Windows sandbox required a runtime-only temporary-directory mode shim; it
is not packaged, committed, or required by the application. Python imports were
pointed at this rebuild. `requirements-tested.txt` records the local Python 3.12
environment; it is not a universal resolver lock.

## Disposition of the review findings

These rows map to the original-source findings in [REVIEW.md](REVIEW.md).
“Implemented” describes the code change and local evidence, not certification of
all external broker behavior.

| Finding | Rebuild response | Remaining boundary |
|---|---|---|
| F01 | Accepted unfilled exits remain tracked; unresolved exits block additional risk | Cancelled/rejected exits and ambiguous timeouts may need operator reconciliation |
| F02 | Thread/process writer lease, ledger reload and atomic state replacement | Local disk only; broker and JSON writes are not one transaction |
| F03 | Submission-time sizing, cash/exposure recalculation and pending reservations | Actual fills, fees and external account activity can differ from estimates |
| F04 | Required missing context fails closed | Optional context is still optional by configuration |
| F05 | Outcome provenance; estimates cannot authorize automatic learning or risk increases | Complete broker execution/fee reconciliation remains future work |
| F06 | Halt persisted before broker work; entry paths recheck the flag | An order already in transit cannot be recalled by a local flag |
| F07 | Daily loss latch survives recovery within the session | Daily-bar research does not replay the intraday latch |
| F08 | Required research regime inputs block when missing; stale forward-fill removed; empty breadth guarded | Historical universe and classifications are not point-in-time datasets |
| F09 | Decimal step precision corrected | Only the existing bounded knob set is supported |
| F10 | Loaded overrides validated against allowlist and bounds | Operator settings still require sound strategy judgment |
| F11 | Proposal-only default; fresh evidence; at most one conservative opt-in tightening; no automatic shadow loosening/reversion | Observational evidence is not causal proof; untouched holdout evaluation remains necessary |
| F12 | Next-session-open execution, affordability/cost allowance, heat/theme limits and failed-breakout exits | Daily bars cannot establish intraday event order or full live parity |
| F13 | Baseline fallback, fold/grid validation and first-day OOS return preserved | Folds reset holdings and are independent experiments |
| F14 | Public-bind password requirement, browser origin validation and trusted ASGI identity | Deployment must configure TLS, trusted proxies and host ACLs correctly |
| F15 | Nonfinite values rejected in sizing/configuration, statistics and relevant validation paths | External schema validation continues at adapter boundaries |
| F16 | Unique atomic temporary files and coordinated paid-request budget reservation | Not a distributed store; health counters remain best-effort telemetry |
| F17 | New overview, journal, navigation and design system; plans/orders/evidence labels corrected | Some advanced legacy forms remain dense; no fabricated performance charts |
| F18 | Correct dev dependency, tested dependency snapshot, CI matrix and container process supervision | Docker and external deployment were not executed locally |

## What was not verified

No live orders, paid-feed calls, model-provider calls, broker paper-account
certification or historical profitability study were performed. The review and
rebuild do not establish improved returns. Partial entry fills, bracket-order
replacement, symbol-wide cancellation, broker-inferred exit prices and ambiguous
API timeouts remain material integration work. See [ARCHITECTURE.md](ARCHITECTURE.md).

The proposed long-term improvements in the review are a roadmap, not a claim
that every feature was delivered. Full execution-event storage, point-in-time
datasets, causal evaluation, uninterrupted portfolio walk-forward simulation
and richer performance visualization remain subsequent projects.

## Reproduce

```bash
python -m pip install -e ".[dev]"
python -m pytest tests -q
python -m pip wheel . --no-deps --wheel-dir dist
```

For the tested dependency versions on Python 3.12, install
`requirements-tested.txt` first. Windows file modes use platform-appropriate
assertions; POSIX private-file checks still require mode 0600.
