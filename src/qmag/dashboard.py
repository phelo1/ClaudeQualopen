"""Read-mostly web dashboard over the trader's state directory.

Shows the market regime, theme leaderboard, tonight's plans with their charts
and checklists, open positions, pending orders, closed trades and the
daemon's heartbeat. Everything comes from the JSON files the session and
daemon write, so it can run on a different machine from the trader as long as
the state directory is shared.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .auth import COOKIE, PASSWORD_ENV, SESSION_SECONDS, DashboardAuth, password_problem
from .health import probe_connections
from .redact import describe_error
from .providers import PRESETS
from .reviewer import list_models, provider_keys, registry
from .session import TradingSession
from .settings import FIELD_HELP, CLEAR_TOKEN, config_from_form, config_from_yaml, config_yaml, describe_config, llm_uses, quick_fields

TEMPLATES = Path(__file__).parent / "templates"
REVIEWER_MODE_LABELS = {"advisory": "advisory", "gate": "gate", "gate_and_size": "gate + size"}
STATE_LABELS = {
    "ok": "OK",
    "degraded": "DEGRADED",
    "error": "ERROR",
    "not_configured": "NOT CONFIGURED",
    "unknown": "NOT CHECKED YET",
    "off": "OFF",
}


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _ago(iso: str | None) -> str:
    """'3 min ago' style relative time for ISO timestamps (UTC assumed if naive)."""
    if not iso:
        return "never"
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - ts).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{secs / 3600:.1f} h ago"
    return f"{secs / 86400:.1f} d ago"


def _reviewer_label(cfg) -> dict | None:
    """Header pill for the LLM reviewer: which model is consulted and whether it can block trades."""
    r = cfg.reviewer
    if not r.enabled:
        return None
    from .reviewer import resolve

    res = resolve(r)
    return {"provider": res.id, "label": res.label, "model": res.model, "mode": r.mode, "has_key": res.configured, "error": res.error}


def _flow_label(cfg) -> dict:
    """Header pill for the paid options-flow scan: off, on, or on without a key."""
    import os

    f = cfg.options_flow
    has_key = bool(os.environ.get("UNUSUAL_WHALES_API_KEY"))
    return {
        "enabled": f.enabled,
        "has_key": has_key,
        "state": "off" if not f.enabled else "on" if has_key else "no key",
        "gate": "require bullish" if f.require_bullish else f"min {f.min_score:+.2f}" if f.min_score is not None else "record only",
        "max_symbols": f.max_symbols_per_cycle,
    }


def _edge_label(cfg) -> dict:
    """Header pill for the Unusual Whales edge score: off, on (with its threshold), or on without a key."""
    import os

    e, f = cfg.edge, cfg.options_flow
    has_key = bool(os.environ.get("UNUSUAL_WHALES_API_KEY"))
    on = e.enabled and f.enabled
    features = sum(1 for w in (e.weights or {}).values() if float(w or 0) > 0)
    return {
        "enabled": on,
        "has_key": has_key,
        "state": "off" if not on else "on" if has_key else "no key",
        "gate": (f"gate ≥ {e.threshold:+.2f}" if has_key else "gate not armed (no key)") if e.gate else "advisory",
        "threshold": e.threshold,
        "min_coverage": e.min_coverage,
        "features": features,
        "max_symbols": min(e.max_symbols_per_cycle, f.max_symbols_per_cycle),
    }


def _insider_summary(scan: dict | None) -> dict | None:
    """Header / index card for the last Saturday scan."""
    if not scan:
        return None
    flagged = scan.get("flagged") or []
    return {
        "generated_at": scan.get("generated_at"),
        "week_start": scan.get("week_start"),
        "week_end": scan.get("week_end"),
        "unavailable": bool(scan.get("unavailable")),
        "flagged": len(flagged),
        "investigate": sum(1 for f in flagged if (f.get("ai") or {}).get("verdict") == "investigate"),
        "top": [{"ticker": f["ticker"], "score": f["score"], "direction": f["direction"], "verdict": (f.get("ai") or {}).get("verdict")} for f in flagged[:5]],
        "data_gaps": scan.get("data_gaps") or [],
    }


def _knobs():
    from .learning import KNOBS

    return KNOBS


def _knob_value(cfg, key: str) -> float:
    from .learning import config_value

    return config_value(cfg, key)


def _learning_summary(report: dict | None, cfg) -> dict:
    """Header / index card for the learning layer."""
    ln = cfg.learning
    out = {
        "enabled": ln.enabled,
        "auto_apply": ln.auto_apply,
        "when": f"{ln.review_weekday.capitalize()} {ln.review_time}",
        "generated_at": None,
        "status": None,
        "trades": 0,
        "lessons": [],
        "adjustments": 0,
        "active": [],
        "shadows": {},
        "data_gaps": [],
    }
    if not report:
        return out
    out.update(
        generated_at=report.get("generated_at"),
        status=report.get("status"),
        trades=report.get("trades", 0),
        lessons=(report.get("lessons") or [])[:4],
        adjustments=len(report.get("adjustments") or []),
        active=[{"key": k, **v} for k, v in (report.get("active_overrides") or {}).items()],
        shadows=report.get("shadows") or {},
        data_gaps=report.get("data_gaps") or [],
    )
    return out


def create_app(session: TradingSession) -> FastAPI:
    app = FastAPI(title="qmag dashboard", docs_url=None, redoc_url=None)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
    env.filters["mode_label"] = lambda m: REVIEWER_MODE_LABELS.get(m, str(m))
    env.filters["state_label"] = lambda s: STATE_LABELS.get(s, str(s).upper())
    env.filters["ago"] = _ago
    from .context.edge import GROUP_LABELS as EDGE_GROUPS

    env.globals["edge_groups"] = EDGE_GROUPS
    session.chart_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(TEMPLATES.parent / "static")), name="static")
    app.mount("/charts", StaticFiles(directory=str(session.chart_dir)), name="charts")
    auth = DashboardAuth(session.state_dir)

    def _client(request: Request) -> str:
        # Proxy forwarding is accepted only by the ASGI server's configured
        # trusted-proxy list. Never trust arbitrary client-supplied headers.
        return request.client.host if request.client else "?"

    def _secure(request: Request) -> bool:
        return request.url.scheme == "https"

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            bearer = auth.enabled and auth.bearer_valid(request.headers.get("authorization"))
            expected = (request.url.scheme, request.url.netloc)
            parsed = urlsplit(origin or "")
            if not bearer and (site == "cross-site" or (origin and (parsed.scheme, parsed.netloc) != expected)):
                return JSONResponse({"error": "Cross-origin changes are refused. Open this desk directly."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    def _safe_next(value: str | None) -> str:
        return value if value and value.startswith("/") and not value.startswith("//") else "/"

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        session.reload_settings()  # a password saved on the settings page applies immediately
        path = request.url.path
        if not auth.enabled or path in ("/login", "/logout") or path == "/healthz":
            return await call_next(request)
        if auth.token_valid(request.cookies.get(COOKIE)) or auth.bearer_valid(request.headers.get("authorization")):
            return await call_next(request)
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"error": "authentication required", "login": "/login"}, status_code=401)
        target = path + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str | None = None) -> HTMLResponse:
        if not auth.enabled or auth.token_valid(request.cookies.get(COOKIE)):
            return RedirectResponse(_safe_next(next), status_code=303)  # type: ignore[return-value]
        return HTMLResponse(env.get_template("login.html").render(next=_safe_next(next), error=error))

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        attempt = str(form.get("password") or "")
        target = _safe_next(str(form.get("next") or "/"))
        ok, message = auth.check_password(attempt, _client(request))
        if not ok:
            return HTMLResponse(env.get_template("login.html").render(next=target, error=message), status_code=401)
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(COOKIE, auth.issue_token(), max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=_secure(request), path="/")
        return resp

    @app.api_route("/logout", methods=["GET", "POST"])
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    run_lock = threading.Lock()
    run_state = {"running": False, "error": None}
    probe_lock = threading.Lock()
    probe_state = {"running": False, "error": None, "finished_at": None}

    def health_summary() -> dict:
        """Header pill: overall connection state and how many enabled connections have issues."""
        c = session.connections()
        return {"overall": c["overall"], "issues": c["issues"], "data_gaps": c["data_gaps"]}

    def settings() -> dict:
        session.reload_settings()  # pick up what another process (daemon / CLI) saved
        from .accounts import desk_name

        return {
            "desk": desk_name(session.state_dir),
            "broker": session.s.broker,
            "live": session.s.live,
            "data": session.s.data,
            "universe": session.s.universe or ("universe/market.txt" if Path("universe/market.txt").exists() else "universe/default.txt"),
            "state_dir": str(session.state_dir),
            "reviewer": _reviewer_label(session.cfg),
            "options_flow": _flow_label(session.cfg),
            "edge": _edge_label(session.cfg),
            "health": health_summary(),
            "halt": session.halt_state(),
            "auth": auth.enabled,
            "public_url": _public_url(),
        }

    def _public_url() -> str | None:
        # Written by deploy/tunnel-watch.sh on the host that runs the tunnel.
        p = session.state_dir / "tunnel_url.txt"
        try:
            url = p.read_text().strip() if p.exists() else ""
        except OSError:
            return None
        return url if url.startswith("https://") else None

    def snapshot() -> dict:
        from .accounts import read_snapshot

        report = session.last_report() or {}
        state = session.state()
        status = _read_json(session.state_dir / "daemon_status.json")
        ledger = _read_json(session.state_dir / "ledger.json") if session.s.broker == "paper" else None
        account = read_snapshot(session.state_dir)
        marks = {p["symbol"]: p for p in (account or {}).get("positions") or []}
        charts = report.get("charts", {})
        plans = report.get("plans", [])
        rejected = report.get("rejected", [])
        for p in plans + rejected:
            p["chart"] = _chart_url(charts.get(p["symbol"]))
        for plan in plans:
            symbol = plan.get("symbol")
            plan["execution_status"] = "Position open" if symbol in state.managed else "Entry pending" if symbol in state.pending else "Research plan"
        positions = list(state.managed.values())
        for pos in positions:
            pos["chart"] = _chart_url(charts.get(pos["symbol"]))
            if pos.get("entry_price") and pos.get("initial_stop") is not None:
                pos["risk_per_share"] = max(pos["entry_price"] - pos["initial_stop"], 1e-9)
            m = marks.get(pos["symbol"]) or {}
            pos["last"], pos["mark_asof"], pos["unrealized"], pos["unrealized_pct"] = m.get("last"), m.get("mark_asof"), m.get("unrealized"), m.get("unrealized_pct")
        pending = list(state.pending.values())
        for pen in pending:
            pen["chart"] = _chart_url(charts.get(pen["symbol"]))
        return {
            "report": report,
            "plans": plans,
            "rejected": rejected,
            "positions": positions,
            "pending": pending,
            "closed": list(reversed(state.closed[-30:])),
            "journal": journal_stats(state.closed, report),
            "sources": source_health(report),
            "status": status,
            "ledger": ledger,
            "account": account,
            "settings": settings(),
            "run": dict(run_state),
            "themes": report.get("themes", []),
            "config": report.get("config", session.cfg.to_dict()),
            "arming": state.arming_sorted(),
            "screens": _read_json(session.state_dir / "screens.json") or {},
            "insider": _insider_summary(session.last_insider_scan()),
            "learning": _learning_summary(session.last_learning_report(), session.cfg),
            "learned_overrides": dict(getattr(session, "learned_overrides", {}) or {}),
        }

    @app.get("/", response_class=HTMLResponse)
    def overview() -> str:
        return env.get_template("overview.html").render(**snapshot())

    @app.get("/desk", response_class=HTMLResponse)
    def index() -> str:
        return env.get_template("index.html").render(**snapshot())

    @app.get("/operations", response_class=HTMLResponse)
    def operations_page() -> str:
        return env.get_template("operations.html").render(settings=settings())

    @app.get("/api/operations")
    def operations_status() -> JSONResponse:
        from .operations import status
        return JSONResponse(status(session.state_dir))

    @app.post("/api/maintenance")
    def maintenance() -> JSONResponse:
        from .operations import housekeeping
        return JSONResponse(housekeeping(session.state_dir, session.cfg.autonomy.retention_days))

    @app.get("/api/autonomy")
    def autonomy_status() -> JSONResponse:
        from .autonomy import read
        return JSONResponse(read(session.state_dir))

    @app.post("/api/autonomy/research")
    def autonomy_research() -> JSONResponse:
        return JSONResponse(session.start_research())

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_page() -> str:
        return env.get_template("monitor.html").render(settings=settings())

    @app.get("/api/monitor")
    def monitored_charts() -> JSONResponse:
        from .monitor import records
        return JSONResponse({"records": records(session.state_dir, session.state())})

    @app.get("/journal", response_class=HTMLResponse)
    def journal_page() -> str:
        snap = snapshot()
        snap["records"] = list(reversed(session.state().closed))
        return env.get_template("journal.html").render(**snap)

    # ------------------------------------------------------------------ #
    # Learning: journal post-mortems, shadow trades, lessons, adjustments
    # ------------------------------------------------------------------ #
    learn_lock = threading.Lock()
    learn_state = {"running": False, "error": None, "finished_at": None}

    @app.get("/learning", response_class=HTMLResponse)
    def learning_page() -> str:
        session.reload_settings()
        state = session.state()
        report = session.last_learning_report()
        shadows = sorted(state.shadow, key=lambda s: (s.get("resolved_on") or s.get("date") or ""), reverse=True)
        return env.get_template("learning.html").render(
            report=report, settings=settings(), run=dict(learn_state), ln=session.cfg.learning, en=session.cfg.entry,
            conn=session.connections(), config=session.cfg.to_dict(), shadows=shadows[:60], learned=dict(getattr(session, "learned_overrides", {}) or {}),
            knobs=[{"key": k.key, "label": k.label, "lo": k.lo, "hi": k.hi, "step": k.step, "kind": k.kind, "current": _knob_value(session.cfg, k.key), "help": FIELD_HELP.get(k.key, "")} for k in _knobs()],
            closed_total=len(state.closed),
            autonomy=__import__("qmag.autonomy", fromlist=["read"]).read(session.state_dir), controls=session.cfg.autonomy,
        )

    @app.get("/api/learning")
    def api_learning() -> JSONResponse:
        return JSONResponse({"report": session.last_learning_report(), "run": dict(learn_state), "learned_overrides": dict(getattr(session, "learned_overrides", {}) or {})})

    @app.post("/api/learning/run")
    def api_learning_run() -> JSONResponse:
        if not learn_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "a learning review is already running"}, status_code=409)

        def worker() -> None:
            learn_state.update(running=True, error=None)
            try:
                session.learn()
            except Exception as exc:  # pragma: no cover - surfaced in UI
                learn_state["error"] = describe_error(exc)
            finally:
                import datetime as _dt

                learn_state.update(running=False, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
                learn_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True})

    @app.post("/api/learning/reset")
    def api_learning_reset() -> JSONResponse:
        removed = session.reset_learning()
        return JSONResponse({"reset": removed})

    # ------------------------------------------------------------------ #
    # Advisor: plain English -> reviewed setting changes. The model only
    # proposes; POST /api/advisor/apply is the operator accepting.
    # ------------------------------------------------------------------ #
    from . import advisor as _advisor

    advisor_lock = threading.Lock()

    def advisor_view() -> dict[str, Any]:
        session.reload_settings()
        transcript = _advisor.load_transcript(session)
        ok, note = _advisor.advisor_available(session.cfg)
        return {
            "transcript": transcript,
            "pending": _advisor.pending_changes(transcript),
            "available": ok,
            "availability_note": note,
            "settings_count": len(_advisor.settings_catalogue(session.cfg, dict(session.s.overrides or {}))),
            "desk": _advisor.desk_summary(session),
            "examples": list(_advisor.EXAMPLES),
            "enabled": session.cfg.advisor.enabled,
            "busy": advisor_lock.locked(),
        }

    @app.get("/advisor", response_class=HTMLResponse)
    def advisor_page() -> str:
        view = advisor_view()
        return env.get_template("advisor.html").render(settings=settings(), adv=session.cfg.advisor, conn=session.connections(), **view)

    @app.get("/api/advisor")
    def api_advisor() -> JSONResponse:
        return JSONResponse(advisor_view())

    @app.post("/api/advisor/ask")
    async def api_advisor_ask(request: Request) -> JSONResponse:
        body = await request.json()
        message = str((body or {}).get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is empty")
        if len(message) > 4000:
            raise HTTPException(status_code=413, detail="message too long (4000 characters)")
        if not advisor_lock.acquire(blocking=False):
            return JSONResponse({"error": "the advisor is still answering the previous message"}, status_code=409)
        try:
            exchange = await run_in_threadpool(_advisor.ask_advisor, session, message)
        finally:
            advisor_lock.release()
        return JSONResponse({"exchange": exchange, "pending": _advisor.pending_changes(_advisor.load_transcript(session))})

    @app.post("/api/advisor/apply")
    async def api_advisor_apply(request: Request) -> JSONResponse:
        body = await request.json()
        ids = [str(i) for i in ((body or {}).get("ids") or [])]
        if not ids:
            raise HTTPException(status_code=400, detail="no change ids")
        with trade_lock:
            result = _advisor.apply_changes(session, ids, by="advisor page")
        return JSONResponse({**result, "pending": _advisor.pending_changes(_advisor.load_transcript(session))})

    @app.post("/api/advisor/dismiss")
    async def api_advisor_dismiss(request: Request) -> JSONResponse:
        body = await request.json()
        ids = [str(i) for i in ((body or {}).get("ids") or [])]
        n = _advisor.dismiss_changes(session, ids)
        return JSONResponse({"dismissed": n, "pending": _advisor.pending_changes(_advisor.load_transcript(session))})

    @app.post("/api/advisor/clear")
    def api_advisor_clear() -> JSONResponse:
        return JSONResponse({"cleared": _advisor.clear_transcript(session)})

    @app.get("/api/snapshot")
    def api_snapshot() -> JSONResponse:
        return JSONResponse(snapshot())

    @app.get("/api/report")
    def api_report() -> JSONResponse:
        return JSONResponse(session.last_report() or {})

    from .broker_test import DEFAULT_SYMBOL as TEST_SYMBOL, KINDS as ORDER_TEST_KINDS, load_results as load_order_tests, run_order_test

    order_lock = threading.Lock()
    order_state = {"running": False, "error": None, "finished_at": None, "kind": None, "symbol": None}

    @app.get("/status", response_class=HTMLResponse)
    def status_page() -> str:
        """Every connection the desk depends on: enabled? configured? working? when did it last answer?"""
        return env.get_template("status.html").render(
            conn=session.connections(), settings=settings(), probe=dict(probe_state), daemon=_read_json(session.state_dir / "daemon_status.json"),
            order_tests=load_order_tests(session.state_dir)[:6], order_run=dict(order_state), test_symbol=TEST_SYMBOL,
        )

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse({**session.connections(), "probe": dict(probe_state), "order_test": dict(order_state)})

    @app.get("/api/status/order-tests")
    def api_order_tests() -> JSONResponse:
        return JSONResponse({"tests": load_order_tests(session.state_dir), "run": dict(order_state)})

    @app.post("/api/status/order-test")
    async def api_order_test(request: Request) -> JSONResponse:
        """Send a test order through the broker: a far-away bracket that is cancelled again, or (paper only) a 1-share round trip."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        kind = str(body.get("kind") or "bracket")
        symbol = str(body.get("symbol") or TEST_SYMBOL).upper().strip()
        if kind not in ORDER_TEST_KINDS:
            raise HTTPException(400, f"kind must be one of {', '.join(ORDER_TEST_KINDS)}")
        if kind == "fill" and session.s.live:
            raise HTTPException(403, "the fill test buys real shares; it is only available on paper accounts (CLI: qmag broker-test --kind fill --allow-live)")
        if not order_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "an order test is already running"}, status_code=409)

        def worker() -> None:
            order_state.update(running=True, error=None, kind=kind, symbol=symbol)
            try:
                res = run_order_test(session, kind, symbol)
                order_state["error"] = None if res["ok"] else res["error"]
            except Exception as exc:  # pragma: no cover - surfaced in UI
                order_state["error"] = describe_error(exc)
            finally:
                import datetime as _dt

                order_state.update(running=False, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
                order_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True, "kind": kind, "symbol": symbol})

    @app.post("/api/status/probe")
    async def api_probe(request: Request) -> JSONResponse:
        """Actively exercise enabled connections and record the results.

        No body (or an empty ``names`` list): every connection, in the
        background - poll ``/api/status`` for ``probe.running``. With
        ``{"names": [...]}``: only those, synchronously, answering with each
        one's fresh record so a single button can show its own result.
        """
        names: list[str] = []
        try:
            body = await request.json()
            names = [str(n) for n in (body or {}).get("names") or []]
        except Exception:
            names = []
        if not probe_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "a probe is already running"}, status_code=409)
        if names:
            try:
                described = await run_in_threadpool(probe_connections, session, names)
            except Exception as exc:
                return JSONResponse({"error": describe_error(exc)}, status_code=500)
            finally:
                probe_lock.release()
            rows = {c["name"]: c for c in described.get("connections", [])}
            return JSONResponse({"connections": {n: rows[n] for n in names if n in rows}, "unknown": [n for n in names if n not in rows]})

        def worker() -> None:
            probe_state.update(running=True, error=None)
            try:
                probe_connections(session)
            except Exception as exc:  # pragma: no cover - surfaced in UI
                probe_state["error"] = describe_error(exc)
            finally:
                import datetime as _dt

                probe_state.update(running=False, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
                probe_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True})

    # ------------------------------------------------------------------ #
    # Accounts: this desk's account and every other desk in QMAG_DESKS
    # ------------------------------------------------------------------ #
    from .accounts import DESKS_ENV, halt_desk

    accounts_lock = threading.Lock()
    accounts_state = {"running": False, "error": None, "finished_at": None}

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page() -> str:
        """Equity, cash, holdings marked at the latest real bar, unrealised and realised P&L - per account and summed per currency."""
        session.reload_settings()
        return env.get_template("accounts.html").render(
            overview=session.accounts(), settings=settings(), run=dict(accounts_state), desks_env=DESKS_ENV, config=session.cfg.to_dict(),
        )

    @app.get("/api/accounts")
    def api_accounts() -> JSONResponse:
        session.reload_settings()
        return JSONResponse({**session.accounts(), "run": dict(accounts_state)})

    @app.post("/api/accounts/refresh")
    def api_accounts_refresh() -> JSONResponse:
        """Rebuild this desk's snapshot from the broker now (other desks refresh themselves after their cycles)."""
        if not accounts_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "a refresh is already running"}, status_code=409)

        def worker() -> None:
            accounts_state.update(running=True, error=None)
            try:
                session.reload_settings()
                snap = session.account_snapshot()
                accounts_state["error"] = snap.get("error")
            except Exception as exc:  # pragma: no cover - surfaced in UI
                accounts_state["error"] = describe_error(exc)
            finally:
                import datetime as _dt

                accounts_state.update(running=False, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
                accounts_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True})

    @app.post("/api/accounts/{name}/halt")
    async def api_accounts_halt(name: str, request: Request) -> JSONResponse:
        """Throw (``{"on": true, "reason": "..."}``) or clear (``{"on": false}``) one desk's kill switch.
        Another desk's switch is its halt file; its daemon honours it on the next pass. Flattening is only offered on the desk itself."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        on = bool(body.get("on", True))
        if not trade_lock.acquire(blocking=False):
            raise HTTPException(409, "another manual order is being placed; wait a moment")
        try:
            result = halt_desk(session, name, on, reason=str(body.get("reason") or "").strip(), by="accounts page")
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0] if exc.args else exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            trade_lock.release()
        return JSONResponse(result)

    @app.get("/plan/{symbol}", response_class=HTMLResponse)
    def plan_detail(symbol: str) -> str:
        """Full justification for a plan (taken or rejected) from the last cycle."""
        report = session.last_report() or {}
        sym = symbol.upper()
        plan = next((p for p in report.get("plans", []) + report.get("rejected", []) if p["symbol"] == sym), None)
        if plan is None:
            raise HTTPException(404, f"{sym} is not in the last cycle's plans; try the symbol lookup")
        sig = next((s for s in report.get("triggered", []) + report.get("watchlist", []) if s["symbol"] == sym), None)
        return env.get_template("detail.html").render(
            symbol=sym,
            title=f"{sym} · {'plan' if plan.get('ok') else 'rejected'}",
            status="triggered" if any(s["symbol"] == sym for s in report.get("triggered", [])) else "watch",
            plan=plan,
            context=plan.get("context"),
            chart=_chart_url(report.get("charts", {}).get(sym)),
            facts=(sig or {}).get("details", {}),
            asof=report.get("asof"),
            regime_ok=bool(report.get("regime_ok", False)),
            regime_known=bool(report.get("regime_known", True)),
            regime_note=report.get("regime_note", ""),
            data_gaps=plan.get("data_gaps") or [],
            settings=settings(),
            config=report.get("config", session.cfg.to_dict()),
            lookup=False,
        )

    def analyze(symbol: str) -> dict:
        try:
            result = session.analyze_symbol(symbol)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        result["chart"] = _chart_url(result.get("chart"))
        return result

    @app.get("/symbol/{symbol}", response_class=HTMLResponse)
    def symbol_lookup(symbol: str) -> str:
        """Desk check for any ticker: setup status, plan, context and chart, computed on demand."""
        r = analyze(symbol)
        report = session.last_report() or {}
        in_report = any(p["symbol"] == r["symbol"] for p in report.get("plans", []) + report.get("rejected", []))
        return env.get_template("detail.html").render(
            report_plan=in_report,
            symbol=r["symbol"],
            title=f"{r['symbol']} · lookup",
            status=r["status"],
            plan=r["plan"],
            context=r["context"],
            chart=r["chart"],
            facts=r["facts"],
            asof=r["asof"],
            regime_ok=r["regime_ok"],
            regime_known=r.get("regime_known", True),
            regime_note=r["regime_note"],
            data_gaps=r.get("data_gaps") or [],
            chart_note=r.get("chart_note"),
            last_close=r["last_close"],
            momentum_leader=r["momentum_leader"],
            theme=r["theme"],
            theme_pct=r["theme_pct"],
            settings=settings(),
            config=session.cfg.to_dict(),
            lookup=True,
        )

    @app.get("/api/symbol/{symbol}")
    def api_symbol(symbol: str) -> JSONResponse:
        return JSONResponse(analyze(symbol))

    trade_lock = threading.Lock()

    @app.post("/api/symbol/{symbol}/trade")
    async def api_symbol_trade(symbol: str, request: Request) -> JSONResponse:
        """Act on a lookup: arm the setup, buy it at market with the plan's stop and target, or override the checks.
        The plan is rebuilt from fresh data first; refusals come back as 409 with the reason and the alternatives."""
        from .session import ManualTradeRefused

        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "buy")
        confirm_live = bool(body.get("confirm_live"))
        if not trade_lock.acquire(blocking=False):
            raise HTTPException(409, {"message": "another manual order is being placed; wait a moment", "can_override": False, "can_arm": False, "can_confirm": False, "problems": []})
        try:
            result = session.manual_trade(symbol, action, confirm_live=confirm_live)
        except ManualTradeRefused as exc:
            raise HTTPException(409, exc.to_dict()) from exc
        except KeyError as exc:
            raise HTTPException(404, {"message": str(exc.args[0] if exc.args else exc), "can_override": False, "can_arm": False, "can_confirm": False, "problems": []}) from exc
        finally:
            trade_lock.release()
        return JSONResponse(result)

    @app.post("/api/alerts/test")
    def api_alerts_test() -> JSONResponse:
        """Send a test alert over every configured channel (Telegram / webhook) and report the outcome."""
        session.reload_settings()
        res = session.alerts.test()
        return JSONResponse(res, status_code=200 if res["ok"] else 502)

    @app.get("/api/halt")
    def api_halt_get() -> JSONResponse:
        return JSONResponse({"halted": session.halt_state()})

    @app.post("/api/halt")
    async def api_halt(request: Request) -> JSONResponse:
        """Kill switch. ``{"on": true, "reason": "...", "flatten": false}`` stops every new entry
        (daemon, dashboard and CLI all honour it); ``flatten`` also sells every open position at market.
        ``{"on": false}`` resumes. Flattening a LIVE account needs ``confirm_live``."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        on = bool(body.get("on", True))
        flatten = bool(body.get("flatten"))
        if on and flatten and session.s.live and not bool(body.get("confirm_live")):
            raise HTTPException(403, "flattening sells every open position on the LIVE account; confirm it explicitly")
        if not trade_lock.acquire(blocking=False):
            raise HTTPException(409, "another manual order is being placed; wait a moment")
        try:
            result = session.halt(on, reason=str(body.get("reason") or "").strip(), by="dashboard", flatten=flatten)
        finally:
            trade_lock.release()
        return JSONResponse(result)

    @app.get("/chart/{symbol}")
    def chart(symbol: str) -> FileResponse:
        report = session.last_report() or {}
        path = report.get("charts", {}).get(symbol.upper())
        if not path or not Path(path).exists():
            raise HTTPException(404, f"No chart for {symbol}")
        return FileResponse(path)

    # ------------------------------------------------------------------ #
    # Saturday insider / unusual-options scan
    # ------------------------------------------------------------------ #
    insider_lock = threading.Lock()
    insider_state = {"running": False, "error": None, "finished_at": None}

    @app.get("/insider", response_class=HTMLResponse)
    def insider_page() -> str:
        scan = session.last_insider_scan()
        return env.get_template("insider.html").render(
            scan=scan, settings=settings(), run=dict(insider_state), ins=session.cfg.insider_scan,
            conn=session.connections(), config=session.cfg.to_dict(),
        )

    @app.get("/api/insider")
    def api_insider() -> JSONResponse:
        return JSONResponse({"scan": session.last_insider_scan(), "run": dict(insider_state)})

    @app.post("/api/insider/run")
    def api_insider_run() -> JSONResponse:
        if not insider_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "an insider scan is already running"}, status_code=409)

        def worker() -> None:
            insider_state.update(running=True, error=None)
            try:
                session.insider_scan()
            except Exception as exc:  # pragma: no cover - surfaced in UI
                insider_state["error"] = describe_error(exc)
            finally:
                import datetime as _dt

                insider_state.update(running=False, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
                insider_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True})

    @app.post("/api/run")
    def api_run() -> JSONResponse:
        if session.s.live:
            raise HTTPException(403, "Manual cycles are disabled for live brokers; use the daemon schedule")
        if not run_lock.acquire(blocking=False):
            return JSONResponse({"started": False, "reason": "a cycle is already running"}, status_code=409)

        run_state.update(running=True, error=None)

        def worker() -> None:
            try:
                session.cycle(max_age_hours=0.0, label="manual")
            except Exception as exc:  # pragma: no cover - surfaced in UI
                run_state["error"] = describe_error(exc)
            finally:
                run_state["running"] = False
                run_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return JSONResponse({"started": True})

    # ------------------------------------------------------------------ #
    # Settings page: strategy parameters + connection credentials, saved in
    # the state directory, exportable / importable as one YAML bundle.
    # ------------------------------------------------------------------ #
    store = session.store

    def settings_page(saved: str | None = None, error: str | None = None, notes: list[str] | None = None, yaml_text: str | None = None) -> str:
        session.reload_settings()
        sections = describe_config(session.cfg)
        return env.get_template("settings.html").render(
            settings=settings(),
            sections=sections,
            quick=quick_fields(sections),
            llm_uses=llm_uses(sections),
            llm_keys=provider_keys(),
            providers=[p.public() for p in registry().all()],
            presets=PRESETS,
            auto_provider=registry().auto().public(),
            conn_states={c["name"]: c for c in session.connections()["connections"] if c["group"] == "llm"},
            env_groups=store.env_groups(),
            yaml_text=yaml_text if yaml_text is not None else config_yaml(session.cfg),
            config_source=session.config_source,
            yaml_path=str(store.yaml_path),
            env_path=str(store.env_path),
            yaml_exists=store.yaml_path.exists(),
            env_exists=store.env_path.exists(),
            saved=saved,
            error=error,
            notes=notes or [],
            clear_token=CLEAR_TOKEN,
            requested_data=session.requested_data,
            cli_config=str(session.s.config) if session.s.config else None,
            cli_overrides=dict(session.s.overrides or {}),
        )

    def _redirect(saved: str | None = None, error: str | None = None, notes: list[str] | None = None) -> RedirectResponse:
        q = []
        if saved:
            q.append("saved=" + quote(saved))
        if error:
            q.append("error=" + quote(error))
        for n in notes or []:
            q.append("note=" + quote(n))
        return RedirectResponse("/settings" + ("?" + "&".join(q) if q else ""), status_code=303)

    async def _form(request: Request) -> dict[str, str]:
        data = await request.form()
        out: dict[str, str] = {}
        for key in data.keys():
            values = data.getlist(key)
            last = values[-1]
            out[key] = last if isinstance(last, str) else ""
        return out

    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request) -> str:
        qp = request.query_params
        return settings_page(saved=qp.get("saved"), error=qp.get("error"), notes=qp.getlist("note"))

    llm_models_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @app.get("/api/llm/providers")
    def api_llm_providers() -> JSONResponse:
        """The AI providers (keys masked) and the presets the add form offers."""
        session.reload_settings()
        return JSONResponse({"providers": [p.public() for p in registry().all()], "presets": PRESETS})

    @app.get("/api/llm/models")
    def api_llm_models(provider: str = "all", base_url: str | None = None, refresh: bool = False) -> JSONResponse:
        """The models each provider's saved key can call - what the settings page offers in the model fields.

        Results are cached for ten minutes per provider (and key); errors are
        redacted before they leave the process.
        """
        session.reload_settings()
        reg = registry()
        wanted = reg.all() if provider in ("all", "auto", "") else [p for p in reg.all() if p.id == provider.lower()]
        out: dict[str, Any] = {"providers": {}, "keys": provider_keys(), "auto": reg.auto().id}
        if provider not in ("all", "auto", "") and not wanted:
            out["error"] = f"unknown provider '{provider}'"
        for p in wanted:
            cache_key = f"{p.id}|{base_url or p.base_url or ''}|{hashlib.sha1((p.api_key or '').encode()).hexdigest()[:10]}"
            hit = llm_models_cache.get(cache_key)
            if hit and not refresh and time.time() - hit[0] < 600:
                out["providers"][p.id] = hit[1]
                continue
            if not p.configured:
                rec = {"ok": False, "models": [], "error": p.missing() or "not configured", "default": p.default_model}
            else:
                try:
                    models = list_models(p, base_url=base_url)
                    rec = {"ok": True, "models": models, "error": None, "default": p.default_model}
                except Exception as exc:
                    rec = {"ok": False, "models": [], "error": describe_error(exc), "default": p.default_model}
            rec["label"] = p.label
            rec["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            llm_models_cache[cache_key] = (time.time(), rec)
            out["providers"][p.id] = rec
        return JSONResponse(out)

    @app.post("/settings/providers")
    async def settings_providers(request: Request) -> RedirectResponse:
        """Add or update one AI provider (name, endpoint, key)."""
        form = await _form(request)
        try:
            prov, notes = session.providers.upsert(form)
        except ValueError as exc:
            return _redirect(error=f"AI provider not saved: {exc}")
        session.reload_settings(force=True)
        llm_models_cache.clear()
        return _redirect(saved="providers", notes=notes or [f"{prov.label} saved"])

    @app.post("/settings/providers/delete")
    async def settings_providers_delete(request: Request) -> RedirectResponse:
        form = await _form(request)
        pid = str(form.get("id") or "")
        try:
            removed = session.providers.delete(pid)
        except ValueError as exc:
            return _redirect(error=str(exc))
        session.reload_settings(force=True)
        llm_models_cache.clear()
        return _redirect(saved="providers", notes=[f"{pid} removed" if removed else f"{pid} was not there"])

    @app.get("/api/settings")
    def api_settings() -> JSONResponse:
        session.reload_settings()
        return JSONResponse({
            "strategy": session.cfg.to_dict(),
            "connections": store.env_view(),  # secrets masked
            "config_source": session.config_source,
            "data_source": session.s.data,
            "broker": session.s.broker,
        })

    @app.post("/settings/strategy")
    async def settings_strategy(request: Request) -> RedirectResponse:
        form = await _form(request)
        try:
            cfg = config_from_form(form, session.cfg)
        except (ValueError, TypeError) as exc:
            return _redirect(error=f"Strategy not saved: {exc}")
        store.save_config(cfg)
        session.reload_settings(force=True)
        return _redirect(saved="strategy")

    @app.post("/settings/yaml")
    async def settings_yaml(request: Request) -> HTMLResponse:
        form = await _form(request)
        text = form.get("yaml", "")
        try:
            cfg = config_from_yaml(text)
        except (ValueError, TypeError) as exc:
            return HTMLResponse(settings_page(error=f"YAML not saved: {exc}", yaml_text=text), status_code=400)
        store.save_config(cfg)
        session.reload_settings(force=True)
        return HTMLResponse(settings_page(saved="yaml"))

    @app.post("/settings/reset")
    def settings_reset() -> RedirectResponse:
        store.reset_config()
        session.reload_settings(force=True)
        return _redirect(saved="reset")

    @app.post("/settings/connections")
    async def settings_connections(request: Request) -> RedirectResponse:
        form = await _form(request)
        try:
            values, changed = store.update_env_from_form(form)
        except ValueError as exc:
            return _redirect(error=f"Connections not saved: {exc}")
        if PASSWORD_ENV in changed and values.get(PASSWORD_ENV):
            problem = password_problem(values[PASSWORD_ENV])
            if problem:
                return _redirect(error=f"Dashboard password not saved: {problem}")
        store.save_env(values)
        session.reload_settings(force=True)
        note = ("updated " + ", ".join(changed)) if changed else "nothing changed"
        if PASSWORD_ENV in changed:
            note += " · every browser must sign in again"
            resp = _redirect(saved="connections", notes=[note])
            if auth.enabled:
                resp.set_cookie(COOKIE, auth.issue_token(), max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=_secure(request), path="/")
            return resp
        return _redirect(saved="connections", notes=[note])

    @app.get("/settings/export")
    def settings_export(secrets: str = "0") -> PlainTextResponse:
        include = secrets in ("1", "true", "on", "yes")
        session.reload_settings()
        text = store.export_bundle(session.cfg, include_secrets=include)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        name = f"qmag-settings-{stamp}{'-with-keys' if include else ''}.yaml"
        return PlainTextResponse(text, media_type="application/x-yaml", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/settings/import")
    async def settings_import(request: Request) -> RedirectResponse:
        data = await request.form()
        upload = data.get("file")
        include = data.get("include_secrets") in ("1", "on", "true")
        if upload is None or isinstance(upload, str):
            return _redirect(error="Import: choose a settings file first")
        raw = await upload.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _redirect(error="Import: the file is not UTF-8 text")
        try:
            result = store.import_bundle(text, include_secrets=include)
        except (ValueError, TypeError) as exc:
            return _redirect(error=f"Import failed, nothing changed: {exc}")
        session.reload_settings(force=True)
        notes = list(result["notes"])
        notes.insert(0, ("strategy replaced" if result["strategy"] else "no strategy section") + "; connections: " + (", ".join(result["connections"]) or "none"))
        return _redirect(saved="import", notes=notes)

    return app


def _chart_url(path: str | None) -> str | None:
    return f"/charts/{Path(path).name}" if path else None


def journal_stats(closed: list[dict], report: dict) -> dict:
    """Win rate / expectancy from the trade journal plus the adaptive-risk state."""
    rs = [float(r["r_multiple"]) for r in closed if r.get("r_multiple") is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "trades": len(rs),
        "win_rate": len(wins) / len(rs) if rs else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "total_r": sum(rs) if rs else 0.0,
        "avg_win_r": sum(wins) / len(wins) if wins else None,
        "avg_loss_r": sum(losses) / len(losses) if losses else None,
        "recent_r": report.get("recent_r", rs[-10:]),
        "risk_mult": report.get("risk_mult", 1.0),
        "pnl": sum(float(r.get("pnl") or 0.0) for r in closed),
    }


SOURCE_LABELS = {
    "fundamentals": "finviz fundamentals",
    "news_finviz": "finviz news",
    "news_yahoo": "Yahoo news",
    "yahoo": "Yahoo calendar",
    "stocktwits": "StockTwits",
    "reddit": "Reddit",
    "unusual_whales": "Unusual Whales flow",
    "uw_edge": "Unusual Whales edge score",
}


def source_health(report: dict) -> list[dict]:
    """Which context sources answered in the last cycle (across all symbols gathered)."""
    ctx = report.get("context") or {}
    if not ctx:
        return []
    seen: dict[str, dict] = {}
    for rep in ctx.values():
        for name, ok in (rep.get("available") or {}).items():
            s = seen.setdefault(name, {"name": name, "label": SOURCE_LABELS.get(name, name), "ok": 0, "total": 0, "error": None})
            s["total"] += 1
            s["ok"] += 1 if ok else 0
            if not ok and rep.get("errors", {}).get(name):
                s["error"] = rep["errors"][name]
    order = list(SOURCE_LABELS)
    return sorted(seen.values(), key=lambda s: order.index(s["name"]) if s["name"] in order else 99)
