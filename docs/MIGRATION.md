# Migration

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
