"""A recurring reconciliation checks account access and records recovery."""
import json

from qmag.broker import Account
from qmag.daemon import Daemon
from qmag.session import SessionSettings, TradingSession


def test_scheduled_account_check_recovers_without_manual_probe(tmp_path, monkeypatch):
    session = TradingSession(SessionSettings(state_dir=tmp_path, broker="paper", data="csv"))
    broker = session.broker
    attempts = []

    def account():
        attempts.append(True)
        if len(attempts) == 1:
            raise ConnectionError("temporary account outage")
        return Account(equity=10000, cash=10000, currency="USD")

    monkeypatch.setattr(broker, "account", account)
    monkeypatch.setattr(session.alerts, "failure", lambda *args, **kwargs: None)
    daemon = Daemon(session, rebuild_universe=False)
    assert daemon.run_task("reconcile") is None
    assert session.health.get("broker")["consecutive_failures"] == 1
    assert json.loads(session.state_path.read_text())["reconciliation"]["ok"]
    task = next(t for t in daemon.tasks if t.name == "reconcile")
    assert "temporary account outage" in task.last_error

    result = daemon.run_task("reconcile")
    assert result["ok"] and task.last_error is None
    record = session.health.get("broker")
    assert record["consecutive_failures"] == 0 and record["last_ok"]
    assert len(attempts) == 2
    assert broker.open_orders() == [] and broker.positions() == {}
