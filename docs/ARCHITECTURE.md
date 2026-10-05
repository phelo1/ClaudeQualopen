# Current architecture and execution boundaries

`TradingSession` coordinates the data, strategy, portfolio state and broker. `run_cycle` detects setups, applies data/regime/context/portfolio gates, ranks and sizes plans, submits entries, and manages stops/targets/time/trend exits. A fixed US equity calendar drives full scans, focused scans and research. Options-flow research supplies stock leads to that same gated workflow.

## Durable execution

Entries, market exits and protective replacements write an intent before submission. Unique client tags let a later pass recover an acknowledgement lost in transport. Unknown orders remain unresolved and block new risk. Cumulative execution snapshots are recorded in `trader.json`; repeated polls do not double-count fills. Partial entries and exits, terminal cancellations/rejections and late commissions are reconciled. Protection replacement requires owned-order cancellation acknowledgement and checks for quantity changes during cancellation. Closed positions cancel residual owned exits.

The current contract is aggregate per order, not a full event-sourced exchange ledger. Persisted terminal snapshots bridge finite API history, but unresolved orders older than available broker history need external reconciliation. Legacy positions without entry IDs are not upgraded to verified executions. Non-USD accounts need valid conversion; unknown conversion blocks new USD risk. Corporate actions, execution corrections/busts, financing and all statement-level adjustments are not fully automated.

| Adapter | Implemented behavior | Remaining external boundary |
|---|---|---|
| IBKR | Selected account, stable client ID, order-reference recovery, open/completed orders, execution/commission aggregation, OCA exits; connection released at the end of the desk writer lease so dashboard and daemon can share the client ID | TWS/Gateway authentication, market permissions, account variants, finite execution history and installed SDK/server versions require account-level validation |
| Alpaca | Client-order-ID recovery, cumulative partial fills, order status/legs, native bracket/OCO cancellation | Order responses do not supply complete commission/fee settlement in this adapter; unknown costs remain unknown and prevent automatic completion of a live canary |
| MT5 | Order/deal history, position-linked stop exits, native attached protective SL, controller-driven partial profit taking, hedge-ticket close sizing, reported currency and costs | Requires Windows terminal and broker-specific symbols/lots. Profit taking depends on controller availability; there is deliberately no independent SELL_LIMIT pretending to be OCO |
| Local paper | Persistent simulated fills, slippage/commission settings, conservative stop-first ambiguity, no reuse of earlier daily extrema on repeated snapshots | No exchange queue, spread/liquidity reconstruction or broker certification |

The adapters are implementation-complete for the common long-equity lifecycle described here, not certified on every broker account/instrument. Use one strategy owner per account/symbol. Order IDs and client IDs must remain stable; do not reset the IBKR sequence or change the desk client ID while positions/intents exist.

The adapter shapes follow official [IBKR order documentation](https://www.interactivebrokers.com/docs/tws-api/doc/introduction), [Alpaca order methods](https://alpaca.markets/sdks/python/api_reference/trading/orders.html) and [MT5 order-ticket/position deal history](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py).

For a non-USD IBKR account without a USD wallet exchange-rate field, valuation can use an IBKR FX midpoint bar no older than 15 minutes. Direct and inverse major-currency pairs are handled explicitly. Missing, future-dated, invalid or stale rates leave the account in its base currency and block new USD risk. This is valuation only, not an FX cash-conversion order. Background research initializes its own event loop and uses a separate read-only IB data client ID (1,000,000 plus its native worker thread ID), leaving the configured foreground data and trading IDs available.

## State and concurrency

IBKR buy stop-limit entries cap the limit at 0.5% above the trigger, rounded down to cents, or the caller's tighter ceiling. The previous approximately 5% offset was rejected by the deployed paper account as too far through the stop price. This conservative adapter policy is distinct from the strategy's maximum-gap eligibility rule and is not a universal IBKR acceptance guarantee. A gap beyond the tighter limit can remain unfilled; the attached protective stop and broker reconciliation remain in force. Reports record the submitted limit. Other broker adapters and daily-bar backtests do not model this IBKR-specific acceptance constraint.

Completed IBKR callbacks may reset `orderId` and `clientId` to zero and omit status fill quantities/prices. Recovery matches the selected account plus the persisted unique intent tag or execution `permId`, preserving the original local order identity. Cumulative execution quantities/prices recover fills; incomplete commission coverage stays unknown. Ambiguous identities or missing execution prices remain reconciliation errors, not assumed cancellations or zero fills.

The IBKR extra requires `ib_async>=2.1.0,<3`. Version 2.0.1 cached an event loop across the process, causing broker and price-data connections from dashboard/research workers to fail with “The future belongs to a different loop”. The [upstream loop resolver](https://ib-api-reloaded.github.io/ib_async/_modules/ib_async/util.html#getLoop) uses the current thread's loop and replaces closed loops. CI installs the real IBKR SDK and tests concurrent broker/data workers with only the network boundary mocked. The tested dependency snapshot pins a compatible SDK and timezone-data combination; reusing the old timezone constraint can make the package resolver select the broken SDK.

JSON/YAML files live on one local disk. Atomic replacement and a reentrant cross-process writer lease coordinate dashboard, CLI and scheduler. This is not a distributed transaction: a process can fail after a broker accepts an order and before the response is stored, which is why durable intents and recovery tags exist. Research uses a separate registry lease. Every prospective account has an observation checkpoint; interrupted observations prevent promotion.

`qmag autopilot` supervises dashboard/daemon processes. The scheduler preserves due jobs across long tasks, runs all simultaneous jobs, coalesces missed repeating ticks and retries failed work with bounded backoff. Reconciliation runs every five minutes independently of market-data loading. Daily housekeeping creates state backups without credentials; retention only deletes expired backup ZIPs.

## Research fidelity

Daily backtests use completed signals and next-session-open entries. Close-dependent exits also fill at the next available open. New entries cannot spend proceeds from exits that occur later that same day. Stops precede targets when daily OHLC cannot determine ordering. Same-day breakeven activation, queue position, partial fills, actual news availability, corporate actions and historical universe membership remain limitations. `legacy_intrabar` retains explicitly labelled legacy entry behavior for comparisons.

`qmag replay` consumes timezone-aware intraday OHLCV bars, aggregates only observations available by the decision time for daily indicators, and executes queued market orders at the next available bar's open. It uses the same trading loop and requires recorded historical context or explicit disabled context. Current language-model reviewers cannot be called during replay. Intraday bars still have internal ambiguity, and a missing bar is not a fabricated fill. Local replay alone cannot promote a strategy.

## Security and agent access

Public dashboard binds require a password. Authentication covers API/static routes with existing login/health exemptions. Cross-origin mutations are rejected; bearer clients are supported. Maintenance endpoints expose structured, scoped actions rather than arbitrary code execution. Secrets are redacted and excluded from maintenance backups. POSIX private files use mode 0600; Windows operators must use appropriate directory ACLs. Risk limits, live opt-in and the persistent kill switch remain authoritative.
