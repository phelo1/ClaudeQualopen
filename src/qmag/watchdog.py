"""Detect a stalled scheduler separately from process liveness."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from .persistence import atomic_json


def heartbeat_limit(task):
    return 180 if task == 'reconcile' else 3600 if task in ('post_open','after_close','intraday','universe','research') else 900 if task else 300


def inspect(directory: Path, now=None):
    now = now or datetime.now(timezone.utc)
    path = Path(directory)/'daemon_status.json'
    try:
        row = json.loads(path.read_text())
        age = (now-datetime.fromisoformat(row['heartbeat'])).total_seconds()
        task = row.get('running')
        # Full-universe work has a finite but larger allowance than broker IO.
        limit = heartbeat_limit(task)
        return {'stalled': age > limit or age < -60, 'age_seconds': age, 'limit_seconds': limit, 'task': task, 'pid': row.get('pid')}
    except (OSError,ValueError,KeyError,TypeError):
        return {'stalled': True, 'reason': 'Scheduler status is missing or invalid'}


def check(directory: Path, recover=False, runner=subprocess.run, now=None):
    now = now or datetime.now(timezone.utc)
    directory=Path(directory)
    result=inspect(directory,now)
    result.update(at=now.isoformat(), recovery='not_needed' if not result['stalled'] else 'required')
    if recover and result['stalled']:
        active=runner(['systemctl','is-active','qmag-daemon'],capture_output=True,text=True,timeout=10).stdout.strip()
        previous=directory/'watchdog.json'
        prior=json.loads(previous.read_text()) if previous.exists() else {}
        last=prior.get('last_restart')
        result['last_restart']=last
        if active not in ('active','failed'):
            result['recovery']='administratively_stopped'
        elif last and (now-datetime.fromisoformat(last)).total_seconds()<900:
            result['recovery']='restart_cooldown'
        else:
            # Fixed service only; never restart IB Gateway or reset trading state.
            runner(['sudo','-n','systemctl','restart','qmag-daemon'],check=True,timeout=160)
            result.update(recovery='restarted',last_restart=now.isoformat())
    elif (directory/'watchdog.json').exists():
        result['last_restart']=json.loads((directory/'watchdog.json').read_text()).get('last_restart')
    atomic_json(directory/'watchdog.json',result)
    return result
