# Autopilot and maintenance runbook

## Operating model

Configure data, broker, account, risk limits and optional research/model/alert services once. Start `qmag autopilot --broker ibkr --data ibkr --state-dir ibkr_paper`. The dashboard listens on loopback port 8765 by default. Use a distinct state directory and broker client ID for each independent desk. The supplied systemd/container deployment can restart processes across reboots; the local supervisor restarts child processes while it remains running.

Scheduled work covers universe rebuilding, market scans, focused setup checks, options research, journal review, autonomous research, five-minute execution reconciliation and daily backups. The initial startup catches up reconciliation, backup and overdue research; it does not replay missed historical trading orders. New scans run on the next applicable market schedule. Configure alert channels in Settings to receive task failures and policy changes. The platform itself does not require a human to approve each qualifying trade or validated promotion after live mode has been authorized.

IBKR is the primary integration target. TWS/Gateway must be logged into the correct account, API-enabled and reachable. Select `IBKR_ACCOUNT` explicitly for multi-account sessions. Broker credentials and authentication are not bypassed. Interactive Brokers documents its own [daily and weekly reauthentication requirements](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication); a completely intervention-free login cannot be promised by application code.

## Human interface

Start at Operations. The attention queue names failed jobs, unhealthy connections and reconciliation issues. Market monitor shows the most recent actual observation date for every chart. Learning lab distinguishes discovery, forward trial, canary and deployed policy. A quiet market may produce no trades or no candidate promotion for long periods; the program does not invent activity to satisfy a training quota.

## Agent interface

| Task | Command / endpoint | Side effects |
|---|---|---|
| Inspect health and recovery actions | `qmag operations --state-dir PATH`; `GET /api/operations` | Read only, no broker calls |
| Inspect active/candidate policy | `GET /api/autonomy` | Read only |
| Inspect charts and levels | `GET /api/monitor` | Read only |
| Inspect current decisions | `GET /api/report` | Read only |
| Discover endpoint schemas | `GET /openapi.json` | Read only |
| Back up state | `qmag housekeeping --state-dir PATH`; `POST /api/maintenance` | Creates ZIP; deletes only expired backup ZIPs |
| Start research | `POST /api/autonomy/research`; `qmag daemon --once research --state-dir PATH` | Historical computation and trial registration; no direct real orders |
| Reconcile execution and repair protection | `qmag daemon --once reconcile --broker ibkr --state-dir PATH` | Broker reads, owned exit cancellation/replacement; no new entries |
| Inspect schedule | `qmag daemon --show-schedule --state-dir PATH` | Read only |

API clients use the existing `Authorization: Bearer <dashboard password>` authentication when configured. Do not put credentials in shell history or source control. Browser users authenticate through login. Same-origin checks apply to browser mutations. No maintenance endpoint runs arbitrary shell commands.

Existing Resend email alerts are supported through `RESEND_API_KEY`, `QMAG_ALERT_EMAIL_FROM` and `QMAG_ALERT_EMAIL_TO`. Email carries warnings and errors; `QMAG_ALERT_EMAIL_TRADES=yes` also enables routine information. Telegram and webhook channels remain available. Credential migration does not prove delivery; no test email is sent automatically by the migration.

`/api/operations` returns `schema_version`, `status`, `issues[{code,detail,action}]`, daemon jobs/heartbeat, reconciliation, counts, autonomy and maintenance metadata. Status `attention` means inspect the described issue; it is not permission to clear a halt or delete an order intent. Read the endpoint result before retrying a mutation; research replies `started:false` if already running.

## Recovery

Before each IBKR connection, a runtime check recreates a missing or closed worker event loop and verifies that the SDK resolves that same loop. An incompatible SDK or a synchronous call on the web server's running loop fails with an actionable diagnostic before connecting. It does not move pending orders between loops or monkey-patch the broker library.

The startup/five-minute reconciliation task also reads the account and updates broker health automatically. It saves execution/protection results before the account check. A temporary failure is recorded; the scheduler retries with backoff (initially 60 seconds, capped at 15 minutes, or the next scheduled check if sooner). The writer lease releases the IBKR socket after each operation, so the retry reconnects cleanly. Successful checks reset consecutive failures and task errors while preserving historical error timestamps. Busy long-running tasks can delay checks; this is not a separate real-time watchdog thread.

Systemd restarts crashed application services. Automatic recovery does not bypass broker login/2FA, invent missing data, upgrade dependencies inside a trading process, or blindly resubmit orders with unknown acknowledgements. Credentials, account restrictions, incompatible installations and external outages can still require intervention; the configured alerts and Operations page expose these failures.

- **Feed outage:** repair connectivity/credentials. Scheduled work retries. Independent reconciliation keeps tracking existing broker orders; required missing prices/context block new entries. MT5 targets cannot execute while the controller is down, though attached stops remain at the terminal/broker.
- **Unknown order acknowledgement:** inspect the persisted order ID/client tag and broker history. The controller retries lookup. Never delete the intent to “unblock” trading; that can create a duplicate order. If history cannot recover it, reconcile with broker records before editing state under a halted desk.
- **Legacy state without execution IDs:** preserve the journal, establish current ownership from broker records, and reconcile before new risk. Historical inferred prices stay estimated.
- **Corrupt state:** stop the supervisor, preserve the corrupt files, inspect a backup, and reconcile restored state against the current broker before resuming. A backup is historical account state, not authority to replay orders.
- **Gateway authentication:** complete the broker's login/security step and let scheduled retries reconnect. Account authentication cannot be delegated to speculative trading logic.
- **Candidate fails validation:** no operator action is needed; it expires or is rejected and the baseline continues. A promoted policy can roll back automatically. Missing known-cost outcomes leave live deployment at canary risk.

Backups contain journal, ledger, policy registry, strategy configuration and current experiment ledgers. They exclude settings.env, provider credentials and login secrets. They stay on the same machine; off-machine backups, host monitoring and disk-capacity management remain deployment responsibilities.

## Intraday research

Use real files named `SYMBOL.csv` with `timestamp,open,high,low,close,volume`. Timestamps must include a timezone and identify bar OPEN time. Supply daily warmup bars separately. Example:

```bash
qmag replay --intraday-dir data/minute --daily-dir data/daily --bar-minutes 1 --config replay.yaml --output experiments/run-001
```

Historical context may be supplied using `--context-file`, a JSON array of `{ "at": "2026-01-02T14:31:00Z", "symbol": "ABC", "report": {...ContextReport fields...} }`. `at` is when the information became available, not a later article revision date. Otherwise explicitly disable context in the replay configuration. Disable reviewer/committee calls during replay. Results retain open positions, pending orders, equity and assumptions. Never merge replay state into a live desk.
