"""Connection registry and data-integrity status.

qmag never fabricates market, account or context data. When a source cannot
be reached the affected reading is left empty and the gap is recorded here,
so the dashboard's *Connections* page, ``qmag status`` and every plan can
say exactly what was (and was not) known when a decision was made.

Two things live in this module:

* ``ConnectionRegistry`` – an append-on-write JSON file
  (``state_dir/connections.json``) where the data providers, broker,
  context sources, LLMs and the cycle itself record every success and
  failure (timestamp, latency, item counts, last error). The daemon, the CLI
  and the dashboard may run as different processes, so the file is the
  shared truth.
* ``describe_connections`` – merges those records with the current config
  and environment into one status per connection:

  ==============  ===========================================================
  ``off``          feature disabled in the config
  ``not_configured`` enabled but missing a key / file / terminal
  ``unknown``      enabled and configured, never exercised yet
  ``ok``           last attempt succeeded
  ``degraded``     partially working (some symbols / endpoints failed, stale)
  ``error``        last attempt failed
  ==============  ===========================================================

``probe_connections`` actively exercises every enabled connection on demand
(the *Test connections* button / ``qmag status --probe``).
"""

from __future__ import annotations

import json
from .persistence import atomic_json, desk_lock
import logging
import os
import platform
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .redact import describe_error, redact_secrets

if TYPE_CHECKING:  # pragma: no cover
    from .config import StrategyConfig
    from .session import SessionSettings, TradingSession

log = logging.getLogger(__name__)

STATES = ("ok", "degraded", "error", "not_configured", "unknown", "off")
PROBE_SYMBOL = "AAPL"  # a liquid, always-listed name for exercising context sources
HEARTBEAT_STALE_SECONDS = 180
PRICE_STALE_TRADING_DAYS = 1  # latest bar older than the last completed session counts as stale


@dataclass(frozen=True)
class ConnectionSpec:
    name: str
    label: str
    group: str  # data | broker | context | llm | system
    feeds: str  # what depends on it
    required: bool = False  # the trader must not open new positions without it


SPECS: tuple[ConnectionSpec, ...] = (
    ConnectionSpec("price_data", "Price data", "data", "setups, indicators, regime, sizing, exits", required=True),
    ConnectionSpec("universe", "Scan universe", "data", "which tickers are scanned", required=True),
    ConnectionSpec("regime_data", "Regime inputs (benchmark / VIX)", "data", "market_regime gate", required=True),
    ConnectionSpec("industry_map", "Industry map (finviz fundamentals file)", "data", "theme momentum by industry"),
    ConnectionSpec("sentiment_csv", "Sentiment CSV", "data", "sentiment gate / ranking"),
    ConnectionSpec("screener", "Market screener (gappers / movers)", "data", "pre-market gap scan, intraday movers sweep"),
    ConnectionSpec("broker", "Broker", "broker", "account equity, positions, orders", required=True),
    ConnectionSpec("broker_orders", "Broker order routing (test orders)", "broker", "proves brackets, market orders and cancels work end to end"),
    ConnectionSpec("news_finviz", "finviz news", "context", "news score, catalysts"),
    ConnectionSpec("fundamentals", "finviz fundamentals (per symbol)", "context", "float, short interest, earnings date"),
    ConnectionSpec("news_yahoo", "Yahoo news", "context", "news score fallback"),
    ConnectionSpec("yahoo", "Yahoo calendar", "context", "earnings date fallback"),
    ConnectionSpec("stocktwits", "StockTwits", "context", "social score"),
    ConnectionSpec("reddit", "Reddit", "context", "social score"),
    ConnectionSpec("unusual_whales", "Unusual Whales options flow", "context", "flow score, unusual trades"),
    ConnectionSpec("uw_edge", "Unusual Whales edge score", "context", "weighted edge score, entry threshold gate"),
    ConnectionSpec("llm_committee", "LLM committee", "llm", "bull / bear / risk chair, three seats"),
    ConnectionSpec("llm_reviewer", "LLM reviewer", "llm", "BUY / SELL / HOLD verdict"),
    ConnectionSpec("insider_scan", "Weekly unusual-options (insider) scan", "context", "Saturday flagged tickers for investigation"),
    ConnectionSpec("llm_insider", "LLM catalyst analyst (insider scan)", "llm", "possible catalyst / speculation per flagged ticker"),
    ConnectionSpec("learning", "Learning review (journal, shadows, adjustments)", "system", "weekly lessons, bounded knob adjustments"),
    ConnectionSpec("llm_learning", "LLM post-mortem coach (learning)", "llm", "what worked / what failed per closed trade"),
    ConnectionSpec("llm_advisor", "LLM desk advisor", "llm", "plain-English requests -> reviewed setting changes"),
    ConnectionSpec("alerts", "Alerts (Telegram / webhook)", "system", "fills, exits, kill switch, limits and failures pushed to you"),
    ConnectionSpec("daemon", "24/7 daemon", "system", "scheduled cycles"),
    ConnectionSpec("cycle", "Trading cycle", "system", "the last run end to end"),
)


def _edge_feature_specs() -> tuple[ConnectionSpec, ...]:
    from .context.edge import FEATURES

    return tuple(ConnectionSpec(f"uw:{f.name}", f.label, "uw", f"edge feature ({f.reads})") for f in FEATURES)


SPECS = SPECS + _edge_feature_specs()
SPEC_BY_NAME = {s.name: s for s in SPECS}
GROUP_LABELS = {
    "data": "Market data", "broker": "Broker", "context": "Context sources", "uw": "Unusual Whales edge features",
    "llm": "Language models", "system": "System",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class ConnectionRegistry:
    """Persisted record of the last outcome per connection. Safe to construct
    with ``path=None`` (in-memory only, used by tests)."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._records: dict[str, dict[str, Any]] = {}
        self.reload()

    # -- persistence -------------------------------------------------------
    def reload(self) -> None:
        """Merge the on-disk file into memory, newest ``last_checked`` wins per
        connection (other processes write the same file; unsaved local
        records are never lost)."""
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("connections registry unreadable (%s); keeping in-memory records", exc)
            return
        if not isinstance(data, dict):
            return
        for name, rec in data.items():
            if not isinstance(rec, dict):
                continue
            mine = self._records.get(name)
            if mine is None or str(rec.get("last_checked") or "") > str(mine.get("last_checked") or ""):
                self._records[name] = rec

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with desk_lock(self.path.parent / ".health-writer"):
            self.reload()
            atomic_json(self.path, self._records)

    # -- recording ---------------------------------------------------------
    def record(
        self,
        name: str,
        ok: bool,
        *,
        detail: str = "",
        error: str | None = None,
        latency_ms: float | None = None,
        items: int | None = None,
        degraded: bool = False,
        save: bool = True,
    ) -> dict[str, Any]:
        """Store the outcome of one attempt against ``name``."""
        if self.path is not None:
            self.reload()  # another process may have written since we loaded
        rec = self._records.setdefault(name, {"attempts": 0, "failures": 0, "consecutive_failures": 0})
        now = _now()
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        rec["last_checked"] = now
        rec["detail"] = redact_secrets(detail) or ""
        rec["latency_ms"] = None if latency_ms is None else round(float(latency_ms), 1)
        rec["items"] = items
        rec["ok"] = bool(ok)
        rec["degraded"] = bool(degraded) if ok else False
        if ok:
            rec["last_ok"] = now
            rec["consecutive_failures"] = 0
        else:
            rec["failures"] = int(rec.get("failures", 0)) + 1
            rec["consecutive_failures"] = int(rec.get("consecutive_failures", 0)) + 1
            rec["last_error"] = redact_secrets(error) or "unknown error"
            rec["last_error_at"] = now
        if save:
            self.save()
        return rec

    def record_result(self, name: str, fn, *, detail: str = "", items_of=None, save: bool = True):
        """Run ``fn()``, record success / failure with latency, re-raise on failure."""
        t0 = time.perf_counter()
        try:
            out = fn()
        except Exception as exc:
            self.record(name, False, detail=detail, error=describe_error(exc), latency_ms=(time.perf_counter() - t0) * 1000, save=save)
            raise
        items = items_of(out) if items_of is not None else None
        self.record(name, True, detail=detail, latency_ms=(time.perf_counter() - t0) * 1000, items=items, save=save)
        return out

    def record_sources(self, reports: Iterable[Any]) -> None:
        """Fold the ``available`` / ``errors`` maps of freshly fetched
        ``ContextReport`` objects into one record per source."""
        seen: dict[str, dict[str, Any]] = {}
        for rep in reports:
            available = getattr(rep, "available", None) or {}
            errors = getattr(rep, "errors", None) or {}
            for name, ok in available.items():
                s = seen.setdefault(name, {"ok": 0, "total": 0, "error": None})
                s["total"] += 1
                if ok:
                    s["ok"] += 1
                elif errors.get(name):
                    s["error"] = errors[name]
            # Each Unusual Whales edge feature is its own connection: a feature
            # that did not apply (no listed options) is neither a hit nor a miss.
            edge = getattr(rep, "edge", None) or {}
            for fname, rec in (edge.get("features") or {}).items():
                if rec.get("applicable") is False:
                    continue
                s = seen.setdefault(f"uw:{fname}", {"ok": 0, "total": 0, "error": None})
                s["total"] += 1
                if rec.get("score") is not None:
                    s["ok"] += 1
                else:
                    s["error"] = rec.get("error") or "no answer"
        for name, s in seen.items():
            if s["ok"] == s["total"]:
                self.record(name, True, detail=f"{s['ok']}/{s['total']} symbols answered", items=s["ok"], save=False)
            elif s["ok"] == 0:
                self.record(name, False, detail=f"0/{s['total']} symbols answered", error=s["error"], items=0, save=False)
            else:
                self.record(name, True, detail=f"{s['ok']}/{s['total']} symbols answered; last error: {s['error']}", items=s["ok"], degraded=True, save=False)
        if seen:
            self.save()

    def record_llm(self, plans: Iterable[Any]) -> None:
        """Record committee / reviewer outcomes carried on plans (dicts or TradePlans)."""
        touched = False
        for p in plans:
            for attr, name in (("committee", "llm_committee"), ("reviewer", "llm_reviewer")):
                v = p.get(attr) if isinstance(p, dict) else getattr(p, attr, None)
                if not v:
                    continue
                touched = True
                who = f"{v.get('provider', '')}/{v.get('model', '')}".strip("/")
                if "error" in v:
                    self.record(name, False, detail=who, error=str(v["error"]), save=False)
                else:
                    self.record(name, True, detail=who, save=False)
        if touched:
            self.save()

    # -- reading -----------------------------------------------------------
    def get(self, name: str) -> dict[str, Any] | None:
        return self._records.get(name)

    def records(self) -> dict[str, dict[str, Any]]:
        if self.path is not None:
            self.reload()
        return dict(self._records)


# --------------------------------------------------------------------------- #
# Enabled / configured per connection from config + environment
# --------------------------------------------------------------------------- #
def _env(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)


def _file_info(path: Path | None) -> tuple[bool, str]:
    if path is None:
        return False, "no path configured"
    p = Path(path)
    if not p.exists():
        return False, f"{p} not found"
    age_h = (time.time() - p.stat().st_mtime) / 3600
    return True, f"{p.name}, updated {age_h / 24:.0f}d ago" if age_h > 48 else f"{p.name}, updated {age_h:.0f}h ago"


def _uw_budget_note() -> str:
    """"; 412 calls today (cap 2,000)" or "; PAUSED: ..." - appended to every Unusual Whales note."""
    from .uw import budget

    u = budget().usage()
    if u["blocked"]:
        return f"; {u['blocked']}"
    return f"; {u['calls']:,} calls today (UTC)" + (f", cap {u['cap']:,}" if u["cap"] else ", no daily cap set")


def _configured(name: str, settings: "SessionSettings", cfg: "StrategyConfig") -> tuple[bool, bool, str]:
    """(enabled, configured, note) for ``name`` before looking at any records."""
    from .fundamentals import FUNDAMENTALS_FILE
    from .universe import DEFAULT_UNIVERSE_FILE, MARKET_UNIVERSE_FILE

    c, f, r = cfg.context, cfg.options_flow, cfg.reviewer
    data = settings.data
    if name == "price_data":
        from .data import ibkr_data_blocked, ibkr_endpoint, ibkr_gateway_reachable

        set_aside = ibkr_data_blocked()
        aside = f" (IBKR bars set aside for now: {set_aside})" if set_aside and data not in ("ibkr", "ib") else ""
        if data in ("yfinance", "yahoo"):
            return True, True, "Yahoo Finance daily bars (free, delayed) with a local CSV cache" + aside
        if data == "unusual_whales":
            ok = _env("UNUSUAL_WHALES_API_KEY")
            thr = int(os.environ.get("UNUSUAL_WHALES_BULK_THRESHOLD") or 300)
            sweep = f"; sweeps over {thr} symbols use Yahoo batches" if thr > 0 else ""
            return True, ok, ("Unusual Whales daily candles (as traded) with a local CSV cache" + sweep + _uw_budget_note() + aside) if ok else "set UNUSUAL_WHALES_API_KEY"
        if data in ("ibkr", "ib"):
            host, port = ibkr_endpoint()
            up = ibkr_gateway_reachable(host, port)
            if up and set_aside:
                return True, False, f"IBKR {host}:{port} answers but refused bars: {set_aside}"
            fb = (os.environ.get("IBKR_DATA_FALLBACK") or "yes").strip().lower() not in ("no", "0", "false", "off")
            note = (
                f"IBKR TWS/Gateway {host}:{port} daily bars (delayed unless a market-data subscription is shared with this account)"
                + ("; symbols IB cannot serve fall back to Yahoo" if fb else "; no Yahoo fallback (IBKR_DATA_FALLBACK=no)")
            )
            return True, up, note if up else f"nothing listening on {host}:{port} - start TWS / IB Gateway (or the ibgateway container)"
        if data in ("mt5", "metatrader"):
            ok = platform.system() == "Windows" or bool(os.environ.get("MT5_PATH"))
            return True, ok, "MetaTrader 5 terminal" if ok else "MetaTrader 5 needs the Windows terminal (MT5_PATH)"
        if data == "csv":
            exists = Path(settings.csv_dir).is_dir()
            return True, exists, f"CSV files in {settings.csv_dir}" if exists else f"{settings.csv_dir} is not a directory"
        return True, False, f"unknown provider {data}"
    if name == "universe":
        if settings.symbols:
            return True, True, f"explicit symbols: {settings.symbols}"
        path = Path(settings.universe) if settings.universe else (MARKET_UNIVERSE_FILE if MARKET_UNIVERSE_FILE.exists() else DEFAULT_UNIVERSE_FILE)
        ok, note = _file_info(path)
        return True, ok, note
    if name == "regime_data":
        if not cfg.regime.enabled:
            return False, True, "regime filter disabled in config"
        parts = [cfg.regime.benchmark]
        if cfg.regime.max_vix is not None:
            parts.append(cfg.regime.vix_symbol)
        return True, True, "needs current bars for " + ", ".join(parts) + (" + breadth over the universe" if cfg.regime.breadth_enabled else "")
    if name == "industry_map":
        if not (cfg.themes.enabled and getattr(cfg.themes, "use_industries", False)):
            return False, True, "industry themes off"
        ok, note = _file_info(FUNDAMENTALS_FILE)
        return True, ok, note
    if name == "sentiment_csv":
        if not cfg.sentiment.enabled:
            return False, True, "sentiment filter off (bring a date,symbol,score CSV to enable)"
        ok, note = _file_info(Path(cfg.sentiment.path) if cfg.sentiment.path else None)
        return True, ok, note
    if name == "broker_orders":
        enabled, ok, note = _configured("broker", settings, cfg)
        return enabled, ok, ("run a test order from the connections page or `qmag broker-test`" if ok else note)
    if name == "broker":
        b = settings.broker
        if b == "paper":
            return True, True, "built-in paper broker (local ledger)"
        if b.startswith("alpaca"):
            ok = _env("ALPACA_API_KEY") and _env("ALPACA_SECRET_KEY")
            return True, ok, f"Alpaca {'live' if b.endswith('live') else 'paper'}" + ("" if ok else " - set ALPACA_API_KEY / ALPACA_SECRET_KEY")
        if b.startswith("ibkr"):
            return True, True, f"IBKR {'live' if b.endswith('live') else 'paper'} via TWS/Gateway {os.environ.get('IBKR_HOST', '127.0.0.1')}:{os.environ.get('IBKR_PORT', '7497')}"
        if b.startswith("mt5"):
            ok = platform.system() == "Windows" or bool(os.environ.get("MT5_PATH"))
            return True, ok, f"MetaTrader 5 {'live' if b.endswith('live') else 'demo'}" + ("" if ok else " - needs the Windows terminal")
        return True, False, f"unknown broker {b}"
    ctx_on = c.enabled
    if name in ("news_finviz", "fundamentals"):
        on = ctx_on and (c.news_enabled or c.events_enabled)
        try:
            import finvizfinance  # noqa: F401

            return on, True, "finvizfinance scraper (free, no key)"
        except ImportError:
            return on, False, "finvizfinance not installed"
    if name == "news_yahoo":
        return ctx_on and c.news_enabled, True, "yfinance news (free); fallback, only called when finviz returns fewer than 3 headlines"
    if name == "yahoo":
        return ctx_on and c.events_enabled, True, "yfinance earnings calendar (free)"
    if name == "stocktwits":
        return ctx_on and c.social_enabled and "stocktwits" in c.social_sources, True, "public symbol stream (free, rate limited)"
    if name == "reddit":
        on = ctx_on and c.social_enabled and "reddit" in c.social_sources
        ok = _env("REDDIT_CLIENT_ID") and _env("REDDIT_CLIENT_SECRET")
        return on, ok, "OAuth app" if ok else "set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
    if name == "unusual_whales":
        on = ctx_on and f.enabled
        ok = _env("UNUSUAL_WHALES_API_KEY")
        return on, ok, ("paid API, up to %d tickers per cycle" % f.max_symbols_per_cycle + _uw_budget_note()) if ok else "set UNUSUAL_WHALES_API_KEY"
    if name == "uw_edge" or name.startswith("uw:"):
        e = cfg.edge
        ok = _env("UNUSUAL_WHALES_API_KEY")
        if name == "uw_edge":
            on = ctx_on and f.enabled and e.enabled
            n_feat = sum(1 for w in (e.weights or {}).values() if float(w or 0) > 0)
            gate = f"gate: score >= {e.threshold:+.2f} with coverage >= {e.min_coverage * 100:.0f}%" if e.gate else "advisory (ranking only, no gate)"
            note = f"{n_feat} weighted features, {gate}, up to {e.max_symbols_per_cycle} tickers per cycle"
            if not f.enabled:
                note = "needs options_flow.enabled (the edge score runs after the flow scan)"
            if not ok:
                note = "set UNUSUAL_WHALES_API_KEY" + (" - until then the edge gate is not armed and plans say so" if e.gate and f.enabled else "")
                return on, ok, note
            return on, ok, note + _uw_budget_note()
        from .context.edge import FEATURE_BY_NAME

        feat = FEATURE_BY_NAME.get(name[3:])
        w = float((e.weights or {}).get(name[3:], 0) or 0)
        on = ctx_on and f.enabled and e.enabled and w > 0
        note = (f"weight {w:g} · {feat.rule}" if feat else "") if ok else "set UNUSUAL_WHALES_API_KEY"
        if w <= 0:
            note = "weight 0 - feature off, endpoint never called"
        return on, ok, note
    if name == "screener":
        sch = cfg.schedule
        on = sch.tiered and (sch.premarket_enabled or sch.movers_enabled)
        if _env("UNUSUAL_WHALES_API_KEY"):
            return on, True, "Unusual Whales stock screener (gap %, relative volume)"
        try:
            import finvizfinance  # noqa: F401

            return on, True, "finviz screener (movers only; pre-market gaps need UNUSUAL_WHALES_API_KEY)"
        except ImportError:
            return on, False, "set UNUSUAL_WHALES_API_KEY or install finvizfinance"
    if name == "insider_scan":
        ins = cfg.insider_scan
        ok = _env("UNUSUAL_WHALES_API_KEY")
        when = f"{ins.weekday.capitalize()} {ins.run_time}, last {ins.lookback_days} days"
        return ins.enabled, ok, (f"Unusual Whales flow alerts + unusual contracts · {when}") if ok else "set UNUSUAL_WHALES_API_KEY"
    if name == "learning":
        ln = cfg.learning
        when = f"{ln.review_weekday.capitalize()} {ln.review_time}; adjustments {'applied automatically' if ln.auto_apply else 'proposed only'}"
        return ln.enabled, True, f"post-mortems, shadow trades, weekly review · {when}"
    if name == "llm_learning":
        ln = cfg.learning
        if not (ln.enabled and ln.llm_enabled):
            return False, True, "AI post-mortems off"
        from .learning import llm_available

        ok, note = llm_available(cfg)
        return True, ok, (note + " · strict-JSON post-mortem per closed trade") if ok else note
    if name == "llm_insider":
        ins = cfg.insider_scan
        if not (ins.enabled and ins.ai_enabled):
            return False, True, "AI catalyst analysis off"
        from .reviewer import resolve

        res = resolve(ins)
        return True, res.configured, f"{res.describe()} · strict-JSON catalyst verdict" if res.configured else (res.error or "not configured")
    if name == "llm_advisor":
        if not cfg.advisor.enabled:
            return False, True, "advisor page off"
        from .advisor import advisor_available

        ok, note = advisor_available(cfg)
        return True, ok, (note + " · proposals applied only when you accept them") if ok else note
    if name == "llm_committee":
        cm = cfg.committee
        if not cm.enabled:
            return False, True, "committee off"
        from .llm import SEATS, seat_config
        from .reviewer import resolve

        notes, missing = [], []
        for seat in SEATS:
            res = resolve(seat_config(cm, seat))
            notes.append(f"{seat}: {res.describe()}")
            if not res.configured:
                missing.append(f"{seat}: {res.error}")
        if missing:
            return True, False, "; ".join(missing)
        return True, True, " · ".join(notes) + (" · veto on" if cm.can_veto else " · advisory")
    if name == "llm_reviewer":
        if not r.enabled:
            return False, True, "reviewer off"
        from .reviewer import resolve

        res = resolve(r)
        return True, res.configured, f"{res.describe()} · mode {r.mode}" if res.configured else (res.error or "not configured")
    if name == "alerts":
        from .alerts import channels_from_env, describe_channels

        return True, bool(channels_from_env()), describe_channels()
    if name == "daemon":
        return True, True, "heartbeat in daemon_status.json"
    if name == "cycle":
        return True, True, "last_report.json"
    return True, True, ""


# --------------------------------------------------------------------------- #
# Merge into statuses
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


# Connections ``probe_connections`` can exercise on their own. The rest are observed, not called:
# the daemon and the cycle report themselves, order routing has its own test, the scans run on schedule.
NOT_PROBEABLE = frozenset({"daemon", "cycle", "broker_orders", "insider_scan", "learning", "screener"})


def _status(spec: ConnectionSpec, enabled: bool, configured: bool, note: str, rec: dict | None) -> dict[str, Any]:
    if not enabled:
        state = "off"
    elif not configured:
        state = "not_configured"
    elif rec is None or rec.get("last_checked") is None:
        state = "unknown"
    elif not rec.get("ok", False):
        state = "error"
    elif rec.get("degraded"):
        state = "degraded"
    else:
        state = "ok"
    rec = rec or {}
    return {
        "name": spec.name,
        "label": spec.label,
        "group": spec.group,
        "group_label": GROUP_LABELS[spec.group],
        "feeds": spec.feeds,
        "required": spec.required,
        "enabled": enabled,
        "configured": configured,
        "testable": enabled and configured and spec.name not in NOT_PROBEABLE,
        "state": state,
        "note": note,
        "detail": rec.get("detail") or "",
        "last_ok": rec.get("last_ok"),
        "last_checked": rec.get("last_checked"),
        "last_error": rec.get("last_error"),
        "last_error_at": rec.get("last_error_at"),
        "latency_ms": rec.get("latency_ms"),
        "items": rec.get("items"),
        "attempts": rec.get("attempts", 0),
        "failures": rec.get("failures", 0),
        "consecutive_failures": rec.get("consecutive_failures", 0),
        "age_seconds": _age_seconds(rec.get("last_checked")),
    }


def describe_connections(settings: "SessionSettings", cfg: "StrategyConfig", state_dir: Path, registry: ConnectionRegistry) -> dict[str, Any]:
    """Everything the status page / ``qmag status`` needs, as plain JSON-able data."""
    records = registry.records()
    out: list[dict[str, Any]] = []
    for spec in SPECS:
        enabled, configured, note = _configured(spec.name, settings, cfg)
        rec = records.get(spec.name)
        if spec.name == "daemon":
            st = _read_json(state_dir / "daemon_status.json")
            rec = _daemon_record(st)
            configured = True
            if rec is None:
                note = "not started in this state directory (cycles can still be run manually)"
        elif spec.name == "cycle":
            rep = _read_json(state_dir / "last_report.json")
            rec = _cycle_record(rec, rep)
        out.append(_status(spec, enabled, configured, note, rec))

    active = [s for s in out if s["enabled"]]
    problems = [s for s in active if s["state"] in ("error", "not_configured")]
    degraded = [s for s in active if s["state"] == "degraded"]
    required_bad = [s for s in problems if s["required"]]
    if required_bad:
        overall = "error"
    elif problems or degraded:
        overall = "degraded"
    elif any(s["state"] == "unknown" for s in active if s["required"]):
        overall = "unknown"
    else:
        overall = "ok"
    report = _read_json(state_dir / "last_report.json") or {}
    from .uw import budget

    return {
        "generated_at": _now(),
        "overall": overall,
        "uw_budget": budget().usage(),
        "data_source": settings.data,
        "broker": settings.broker,
        "live": settings.live,
        "counts": {s: sum(1 for c in out if c["state"] == s) for s in STATES},
        "issues": len(problems) + len(degraded),
        "connections": out,
        "groups": [{"key": g, "label": GROUP_LABELS[g], "connections": [c for c in out if c["group"] == g]} for g in GROUP_LABELS],
        "data_gaps": report.get("data_gaps", []),
        "report_asof": report.get("asof"),
        "report_generated_at": report.get("generated_at"),
        "python": sys.version.split()[0],
    }


def _daemon_record(status: dict | None) -> dict | None:
    if not status:
        return None
    age = _age_seconds(status.get("heartbeat"))
    failing = [t for t in status.get("tasks", []) if t.get("last_error")]
    rec = {"last_checked": status.get("heartbeat"), "attempts": sum(int(t.get("runs", 0)) for t in status.get("tasks", [])), "failures": len(failing)}
    if age is None or age > HEARTBEAT_STALE_SECONDS:
        rec.update(ok=False, last_error=f"no heartbeat for {age / 60:.0f} min" if age else "no heartbeat", last_error_at=status.get("heartbeat"), detail="process stopped or hung")
    elif failing:
        rec.update(ok=True, degraded=True, last_ok=status.get("heartbeat"), detail="alive; failing tasks: " + ", ".join(f"{t['name']} ({t['last_error']})" for t in failing), last_error=failing[-1]["last_error"], last_error_at=failing[-1].get("last_run"))
    else:
        nxt = status.get("next_task") or {}
        rec.update(ok=True, last_ok=status.get("heartbeat"), detail=f"alive; next {nxt.get('name', '?')} at {str(nxt.get('at', ''))[11:16]} NY" if nxt else "alive")
    return rec


def _cycle_record(rec: dict | None, report: dict | None) -> dict | None:
    if not report:
        return rec
    rec = dict(rec or {})
    gaps = report.get("data_gaps") or []
    rec.setdefault("last_checked", report.get("generated_at"))
    if rec.get("ok", True):
        rec["ok"] = True
        rec.setdefault("last_ok", report.get("generated_at"))
        rec["degraded"] = bool(gaps)
        rec["detail"] = f"{report.get('label', 'cycle')} as of {report.get('asof')}" + (f"; data gaps: {'; '.join(gaps)}" if gaps else "")
    return rec


# --------------------------------------------------------------------------- #
# Active probes
# --------------------------------------------------------------------------- #
def _ping_openai_compatible(base_url: str | None, key: str | None, timeout: int = 15, model: str | None = None) -> str:
    """Kept for callers that hold a bare URL + key; seats go through :func:`qmag.reviewer.probe_model`."""
    from .providers import Provider
    from .reviewer import Resolved, probe_model

    prov = Provider(id="adhoc", label=base_url or "endpoint", kind="openai", base_url=base_url or "https://api.openai.com/v1", api_key=key)
    if not prov.configured:
        raise RuntimeError("no API key")
    return probe_model(Resolved(prov, model), timeout=timeout)


def _ping_gemini(model: str, key: str, timeout: int = 15) -> str:
    from .providers import Provider
    from .reviewer import Resolved, probe_model

    if not key:
        raise RuntimeError("no API key saved for Google Gemini")
    return probe_model(Resolved(Provider(id="gemini", label="Google Gemini", kind="gemini", api_key=key), model), timeout=timeout)


def probe_connections(session: "TradingSession", names: Iterable[str] | None = None) -> dict[str, Any]:
    """Exercise every enabled + configured connection now and record the outcome.

    Returns ``describe_connections`` afterwards. Paid sources (Unusual
    Whales, LLMs) are only touched when they are enabled in the config.
    """
    from .data import make_provider
    from .universe import load_universe

    reg = session.health
    cfg, s = session.cfg, session.s
    wanted = set(names) if names else {sp.name for sp in SPECS}
    statuses = {sp.name: _configured(sp.name, s, cfg) for sp in SPECS}

    def want(name: str) -> bool:
        enabled, configured, _ = statuses[name]
        return name in wanted and enabled and configured

    if want("broker"):
        try:
            reg.record_result("broker", session.account, detail=f"{s.broker}: account read", items_of=lambda a: None)
        except Exception:
            pass
    if want("price_data"):
        bench = cfg.regime.benchmark if cfg.regime.enabled else PROBE_SYMBOL
        start = str((datetime.now(timezone.utc) - timedelta(days=30)).date())
        t0 = time.perf_counter()
        try:
            provider = make_provider(s.data, directory=s.csv_dir, cache_dir=s.cache_dir, max_age_hours=0.0)
            frames = provider.load([bench], start=start)
            df = frames.get(bench)
            if df is None or df.empty:
                reg.record("price_data", False, detail=f"{s.data}: no bars returned for {bench}", error=f"no bars for {bench}", latency_ms=(time.perf_counter() - t0) * 1000)
            else:
                last = df.index[-1].date()
                stale = _price_stale(last)
                pstats = dict(getattr(provider, "stats", {}) or {})
                served_by = f" (served by {pstats['source']}: {pstats['ibkr_error']})" if pstats.get("ibkr_error") else ""
                reg.record(
                    "price_data", True, detail=f"{s.data}: {bench} latest bar {last}" + served_by + (f" (STALE: expected {expected_last_session()})" if stale else ""),
                    latency_ms=(time.perf_counter() - t0) * 1000, items=len(df), degraded=stale or bool(served_by),
                )
                if cfg.regime.enabled and want("regime_data"):
                    reg.record(
                        "regime_data", not stale, detail=f"{bench} bars through {last}; breadth is evaluated in each cycle over the whole universe",
                        error=f"{bench} latest bar {last} is stale (expected {expected_last_session()})" if stale else None, items=len(df),
                    )
        except Exception as exc:
            reg.record("price_data", False, detail=f"{s.data}: probe failed", error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000)
    if want("universe"):
        try:
            reg.record_result("universe", lambda: load_universe(s.universe, s.symbols), detail="universe file read", items_of=len)
        except Exception:
            pass
    if want("industry_map"):
        from .fundamentals import load_fundamentals

        try:
            reg.record_result("industry_map", load_fundamentals, detail="fundamentals.csv read", items_of=lambda df: 0 if df is None else len(df))
        except Exception:
            pass
    if want("sentiment_csv"):
        try:
            import pandas as pd

            reg.record_result("sentiment_csv", lambda: pd.read_csv(cfg.sentiment.path), detail="sentiment CSV read", items_of=len)
        except Exception:
            pass
    context_names = {"news_finviz", "fundamentals", "news_yahoo", "yahoo", "stocktwits", "reddit", "unusual_whales", "uw_edge"}
    context_names |= {sp.name for sp in SPECS if sp.group == "uw"}
    if session.gatherer is not None and any(want(n) for n in context_names):
        try:
            rep = session.gatherer.one(PROBE_SYMBOL, flow=want("unusual_whales"), edge=want("uw_edge"))
            reg.record_sources([rep])
        except Exception as exc:  # pragma: no cover - one() never raises, belt and braces
            for n in context_names:
                if want(n):
                    reg.record(n, False, error=f"{type(exc).__name__}: {exc}", save=False)
            reg.save()
    from .reviewer import probe_model, resolve

    if want("llm_committee"):
        from .llm import SEATS, seat_config

        def _ping_seats() -> str:
            answers = []
            for seat in SEATS:
                res = resolve(seat_config(cfg.committee, seat))
                try:
                    probe_model(res)
                except Exception as exc:
                    raise RuntimeError(f"{seat} seat: {exc}") from exc
                answers.append(f"{seat}: {res.describe()}")
            return " · ".join(answers)

        try:
            reg.record_result("llm_committee", _ping_seats, detail="three seats: model lookup each")
        except Exception:
            pass
    for conn, use_cfg in (("llm_reviewer", cfg.reviewer), ("llm_insider", cfg.insider_scan), ("llm_learning", cfg.learning), ("llm_advisor", getattr(cfg, "advisor", None))):
        if use_cfg is None or not want(conn):
            continue
        res = resolve(use_cfg)
        try:
            reg.record_result(conn, lambda res=res: probe_model(res), detail=f"{res.describe()}: model lookup")
        except Exception:
            pass
    if want("alerts"):
        session.alerts.test()
    return describe_connections(s, cfg, session.state_dir, reg)


# --------------------------------------------------------------------------- #
# Price freshness
# --------------------------------------------------------------------------- #
def expected_last_session(now: datetime | None = None) -> date:
    """The most recent *completed* NYSE session (America/New_York).

    Today counts only from 30 minutes after the close, so an intraday cycle
    that still shows yesterday's final bar is not flagged; a feed that is a
    whole session behind is.
    """
    from .market_calendar import NY, is_trading_day, market_close

    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    d = now.date()
    if not is_trading_day(d) or now.time() < (datetime.combine(d, market_close(d)) + timedelta(minutes=30)).time():
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _price_stale(last_bar: date, now: datetime | None = None) -> bool:
    return last_bar < expected_last_session(now)


def price_freshness(frames: dict[str, Any], asof: str | None = None) -> dict[str, Any]:
    """How current the loaded price data is: latest bar, symbols on it, stale flag.

    With an explicit ``asof`` (replays) staleness is judged against that date.
    """
    import pandas as pd

    if not frames:
        return {"latest": None, "symbols": 0, "on_latest": 0, "stale": True, "expected": str(expected_last_session()), "note": "no price data loaded"}
    last_dates = [df.index[-1] for df in frames.values() if len(df)]
    latest = max(last_dates)
    on_latest = sum(1 for d in last_dates if d == latest)
    expected = pd.Timestamp(asof).normalize() if asof else pd.Timestamp(expected_last_session())
    stale = latest.normalize() < expected
    note = ""
    if stale:
        note = f"latest bar is {latest.date()}, expected {expected.date()}: price data is stale, no new entries"
    elif on_latest < len(last_dates) * 0.5:
        note = f"only {on_latest}/{len(last_dates)} symbols have a {latest.date()} bar"
    return {"latest": str(latest.date()), "symbols": len(last_dates), "on_latest": on_latest, "stale": bool(stale), "expected": str(expected.date()), "note": note}
