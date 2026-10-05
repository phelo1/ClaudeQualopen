# Migration

## Reduced-risk regime settings

Legacy `regime.breadth_mode`, `regime.breadth_scale` and `regime.risk_off_ep_scale` are preserved when importing saved settings. The earlier rebuild omitted these fields and therefore substituted a hard breadth veto. Existing rebuilt desks require an explicit operator restoration of these values; package defaults remain `gate` and no risk-off EP exception. The Oracle desk's authorized values are `scale`, `0.5`, and `0.5` respectively.

Multipliers reduce the stop-based per-trade risk budget; position, cash, heat and daily-loss limits still apply independently. Two simultaneous 50% reductions combine to 25%, and any canary/adaptive reduction compounds further. The EP exception cannot override unknown inputs or a failed enabled VIX gate. The benchmark still uses its configured moving average; this change does not restore the legacy optional MA hysteresis band.

Focused scans now require dated, structured breadth evidence from a recent full scan with the same breadth moving-average length. Old reports with only a combined regime boolean fail closed until a full-universe measurement is available. The current benchmark is re-evaluated rather than inheriting yesterday's combined failure. A pending order whose permitted risk changes is reconsidered even outside the focused scan's proximity window.

Back up configuration and state before deployment. A maintenance preview may recompute structured breadth from the complete cached universe and seed that evidence in the prior full report, without submitting orders or changing its historical plans. Preserve the source date, record the refresh timestamp, and reject incomplete/mismatched inputs. Normal scheduled cycles then refresh decisions under the restored policy.

1. Stop the original daemon/dashboard and back up the whole state directory.
2. Install this repository in a fresh Python environment; leave the source repository unchanged.
3. Start with a new paper state directory and configured real CSV/market data. Verify account snapshots, connection status and the desk.
4. To inspect history, copy the old state into a separate directory. Missing evidence labels remain estimated/legacy; migration never fabricates verification.
5. Review settings. A saved `learning.auto_apply: true` remains an explicit preference; set it false to adopt proposal-only operation. Unknown/out-of-bounds learned keys are ignored. Reset stale overrides if appropriate.
6. Compare the broker's actual positions/orders with the migrated journal before permitting new submissions.
7. Configure `QMAG_DASHBOARD_PASSWORD` for network binds, TLS at a reverse proxy, and the proxy's trusted forwarding addresses. Secure the state directory, especially its credential files.

The new home page is Overview; the detailed desk is `/desk`. Existing API routes remain. `/journal` adds evidence labels and filtering. Light/dark themes and mobile navigation are supported.

Backtest numbers change because next-session-open fills are now the default. Record the execution model; rerun the same data/settings for valid comparisons. Installing the repository does not migrate credentials, operating state, broker positions or running services.

For pending/rejected exits, inspect the broker before changing a marker or resubmitting. An order may still execute. Broker-specific recovery and execution-history import remain operational responsibilities.


## Version 0.3 autonomy migration

Autonomy and auto-promotion are enabled by default; the scheduler must run and evidence gates must pass before any policy can change. To retain manual policy control, set `autonomy.auto_promote: false`; to keep the legacy learner, set `autonomy.enabled: false`. Legacy `learning.auto_apply` is ignored while autonomy is enabled.

Back up state before migration. New execution IDs, client tags, cumulative snapshots, fee status and policy IDs are additive. Old live positions without order IDs require broker reconciliation; the application does not invent their history. Paper extrema tracking changes simulation results deliberately. MT5 synthetic sell-limit targets are no longer created; existing deployments must inspect and cancel old orphan targets before running the new controller.

IBKR now selects one account and scopes execution to a stable client ID. Configure `IBKR_ACCOUNT` when necessary and keep existing desk client IDs unchanged. Use a separate state directory for replay experiments. The new supervisor is optional; do not start it beside an already-running dashboard on the same port or another daemon on the same desk.
