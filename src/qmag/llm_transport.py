"""Shared bounded cooldowns for optional AI providers; never invent a reply."""
import hashlib
import json as jsonlib
import os
from pathlib import Path
import time
import requests
from .persistence import atomic_json, desk_lock


def post(url, *, headers, json, timeout):
    root=Path(os.environ.get('QMAG_LLM_RUNTIME_DIR','data/cache/llm'))
    # A fingerprint, never the credential itself, identifies the quota owner.
    credential=headers.get('Authorization') or headers.get('x-goog-api-key','')
    identity=hashlib.sha256((url.split('/v1')[0]+credential).encode()).hexdigest()[:24]
    path=root/(identity+'.json')
    with desk_lock(root/identity,timeout=min(timeout,30)):
        row=jsonlib.loads(path.read_text()) if path.exists() else {}
        now=time.time()
        if row.get('until',0)>now:
            raise RuntimeError(f"AI provider cooling down after HTTP {row.get('status')}; retry in {int(row['until']-now)+1}s")
        try:
            response=requests.post(url,headers=headers,json=json,timeout=timeout)
        except requests.RequestException:
            atomic_json(path,{'until':now+60,'status':'network error'})
            raise
        status=getattr(response,'status_code',200)
        delay=3600 if status in (401,402,403) else 300 if status==429 else 60 if status>=500 else 0
        if delay:
            try:
                delay=max(delay,float(response.headers.get('Retry-After',0)))
            except (ValueError,TypeError,AttributeError):pass
            atomic_json(path,{'until':time.time()+delay,'status':status})
        elif path.exists():
            path.unlink()
        return response
