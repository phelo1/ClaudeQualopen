# ClaudeQual

**A momentum research workspace with clearer decisions, stricter execution boundaries, and evidence-led improvement.**

ClaudeQual scans real market data for breakout and episodic-pivot setups, builds explainable trade plans, manages a paper or explicitly enabled broker account, and reviews the decision record. The `qmag` CLI remains compatible with the original project.

This rebuild follows a whole-repository review of [Claude_Qual](https://github.com/phelo1/Claude_Qual) at commit `eae50d69c3aeee7d8c376773950eb56267258541`. Read the [full review](docs/REVIEW.md), [file coverage](docs/REVIEW_COVERAGE.md), and [implementation status](docs/VERIFICATION.md).

## What changed

- **A redesigned workspace:** responsive sidebar navigation, light/dark themes, a focused daily overview, detailed trading desk, portfolio, filterable journal, learning lab and connection controls.
- **Clear execution states:** research plans, pending orders, positions and unresolved exits are distinct. An accepted sell is no longer booked as a completed trade.
- **More reliable state:** session writer leases coordinate CLI/daemon/dashboard, paper ledgers refresh, and critical files use atomic replacement. Emergency halts persist before broker calls.
- **Risk checks at submission:** remaining cash/exposure are rechecked, pending commitments are reserved, the daily loss stop stays latched for the session, and required missing context blocks entries.
- **More honest research:** completed daily signals fill at the following session's open by default. Missing regime inputs block research entries. Unqualified optimization folds retain the baseline.
- **Evidence-aware learning:** proposal-only by default, explicit outcome provenance, fresh evidence after changes, bounded overrides, and restricted opt-in tightening. Shadows cannot automatically loosen or revert settings.
- **Operational checks:** browser origin validation, public-bind password requirement, safer Windows process checks, cross-platform CI and regression coverage.

![Redesigned daily overview](docs/images/dashboard-desktop.png)

[Mobile preview](docs/images/dashboard-mobile.png)

## Run locally

Python 3.11+; Python 3.12 was used for local verification.

```bash
git clone https://github.com/phelo1/ClaudeQualopen.git
cd ClaudeQualopen
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
qmag dashboard --host 127.0.0.1 --port 8765 --data csv --state-dir paper_state
```

Open [localhost:8765](http://127.0.0.1:8765). An empty workspace shows no fabricated returns or market readings. Configure real CSV data or a market-data connection in Settings, verify Connections, then run a paper cycle from the Trading desk. CSV defaults to `data/csv`; use `--csv-dir` for another directory. No broker/model credentials are bundled.

For the locally tested Python 3.12 dependency snapshot, install `requirements-tested.txt` before the editable package. Other Python versions should resolve from `pyproject.toml` and pass CI before use.

```bash
python -m pip install -r requirements-tested.txt
python -m pip install -e ".[dev]"
python -m pytest -q
qmag --help
```

The daemon is a separate, explicit operation; opening the dashboard does not start scheduled trading:

```bash
qmag daemon --broker paper --state-dir paper_state --data csv
```

Optional adapters: `pip install -e ".[alpaca]"`, `.[ibkr]`, or `.[mt5]` on Windows. Public binds require `QMAG_DASHBOARD_PASSWORD`; keep local development on loopback. The container supervises the dashboard and daemon together and stops if either exits.

## How it works

Real inputs → freshness checks → setup detection → deterministic gates and sizing → optional AI review → final risk/halt checks → broker acknowledgement → execution reconciliation → journal → research proposals.

The “learning” layer adjusts a small threshold allowlist. It **does not train model weights**, and AI explanations do not establish a profitable edge. Paper fills, estimated outcomes and shadow simulations have different evidentiary value. See [learning policy](docs/LEARNING.md).

## Documentation

- [Whole-system review](docs/REVIEW.md): goal, strategy, architecture, incentives, implementation, risks and UI assessment.
- [Architecture and limitations](docs/ARCHITECTURE.md): state guarantees, broker boundaries, research assumptions and security.
- [Learning and evaluation](docs/LEARNING.md): what changes, why, and how to test improvements credibly.
- [Migration](docs/MIGRATION.md): existing state/config compatibility and changed defaults.
- [Verification and remaining work](docs/VERIFICATION.md): reproduced checks and unresolved external integration risks.
- [Historical command/provider reference](docs/LEGACY_REFERENCE.md): original detailed manual, explicitly superseded where behavior changed.

The local suite uses synthetic fixtures and mocked external services. Live broker execution, paid feeds, real market performance and Docker deployment require environment-specific validation. There is no claim that this rebuild improves investment returns or is certified for unattended live trading.
