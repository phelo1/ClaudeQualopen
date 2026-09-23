"""Structured maintenance for operators and automation clients.

Read-only status performs no network calls. Housekeeping only snapshots a
fixed allowlist; it never edits journal records, orders, credentials or risk.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import zipfile

from .persistence import atomic_json, desk_lock


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def status(directory: Path) -> dict:
    from .autonomy import read
    directory = Path(directory)
    now = datetime.now(timezone.utc)
    book = read_json(directory / "trader.json", {})
    daemon = read_json(directory / "daemon_status.json", {})
    issues = []
    heartbeat = daemon.get("heartbeat")
    age = (now - datetime.fromisoformat(heartbeat).astimezone(timezone.utc)).total_seconds() if heartbeat else None
    if age is None or age > 300:
        issues.append({"code": "daemon_not_healthy", "detail": "No recent scheduler heartbeat", "action": "Start or inspect qmag autopilot and its service log"})
    recon = book.get("reconciliation", {})
    for issue in recon.get("issues", []):
        issues.append({"code": "broker_reconciliation", "detail": issue, "action": "Inspect broker order IDs and connectivity; do not erase execution state"})
    for task in daemon.get("tasks", []):
        if task.get("last_error"):
            issues.append({"code": "task_failed", "detail": f"{task['name']}: {task['last_error']}", "action": "Repair the reported dependency; scheduler will retry"})
    for name, connection in read_json(directory / "connections.json", {}).items():
        if not connection.get("ok", True):
            issues.append({"code": "connection_failed", "detail": f"{name}: {connection.get('last_error', 'unavailable')}", "action": "Inspect Connections and repair the reported service or credentials"})
        elif connection.get("degraded"):
            issues.append({"code": "connection_degraded", "detail": f"{name}: {connection.get('detail') or 'partial data availability'}", "action": "Inspect Connections for partial feed failures; successful requests do not imply full coverage"})
    halt = read_json(directory / "halt.json", {})
    return {"schema_version": 1, "at": now.isoformat(), "status": "attention" if issues else "healthy",
            "issues": issues, "heartbeat_age_seconds": age, "daemon": daemon, "halt": halt,
            "reconciliation": recon, "positions": len(book.get("managed", {})), "pending_entries": len(book.get("pending", {})),
            "closed_trades": len(book.get("closed", [])), "autonomy": read(directory),
            "maintenance": read_json(directory / "maintenance.json", {}),
            "actions": {"status": "GET /api/operations", "charts": "GET /api/monitor", "research": "POST /api/autonomy/research", "backup": "POST /api/maintenance"}}


def housekeeping(directory: Path, retention_days: int = 180) -> dict:
    directory = Path(directory).resolve()
    with desk_lock(directory), desk_lock(directory / "autonomy"):
        now = datetime.now(timezone.utc)
        root = directory / "backups"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"state-{now.strftime('%Y%m%dT%H%M%S%fZ')}.zip"
        # Explicit allowlist: settings.env / auth tokens are never exported.
        sources = [directory / name for name in ("trader.json", "ledger.json", "autonomy.json", "settings.yaml", "halt.json", "last_report.json")]
        sources += list((directory / "autonomy").glob("*/*/trader.json"))
        sources += list((directory / "autonomy").glob("*/*/ledger.json"))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sources:
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory):
                    if path.suffix == ".json":
                        read_json(path)  # reject corrupt input instead of claiming a valid backup
                    archive.write(path, path.relative_to(directory).as_posix())
        removed = 0
        for path in root.glob("state-*.zip"):
            if path != target and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < now - timedelta(days=max(7, retention_days)):
                path.unlink()
                removed += 1
        result = {"at": now.isoformat(), "backup": target.name, "files": len([p for p in sources if p.is_file()]), "expired_backups_removed": removed}
        atomic_json(directory / "maintenance.json", result)
        return result
