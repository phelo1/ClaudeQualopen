"""A ``TradingSession`` bundles data source, config, broker and state directory.

It is the one place that knows how to run "a cycle" end to end - load and
refresh prices, run the trader, persist state, render charts for every plan
and open position, and write ``last_report.json`` - so the CLI, the 24/7
daemon and the dashboard all behave identically.

Data integrity rules enforced here:

* every load / broker call / context fetch / LLM call is recorded in the
  ``ConnectionRegistry`` (``state_dir/connections.json``) with its outcome;
* price data that is a session behind blocks *new* entries (exits are still
  managed) and the gap is written into the report and every plan;
* there is no simulated price source: ``--data synthetic`` is refused before
  anything is loaded (see ``data.resolve_data_kind``);
* an unreadable broker account never falls back to an assumed balance.

Operator settings (strategy YAML and connection credentials) live in the
state directory - ``settings.yaml`` / ``settings.env`` - and are edited from
the dashboard's settings page (``qmag.settings``). A session applies them on
start and re-reads them at the start of every cycle when they changed, so a
running daemon picks up what was saved in the browser.
"""

from __future__ import annotations

import json
from .persistence import atomic_text, atomic_json, serialized
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from .alerts import Alerter
from .backtest import prepare_data
from .broker import Broker, LIVE_BROKERS, make_broker
from .charts import ChartLevels, chart_signal, render_chart
from .config import StrategyConfig
from .context import ContextGatherer
from .data import DataProvider, make_provider, resolve_data_kind
from .halt import halt_reason, halt_status, set_halt
from .health import ConnectionRegistry, describe_connections, price_freshness
from .market_calendar import NY
from .regime import regime_snapshot
from .providers import ProviderStore
from .settings import SettingsStore
from .setups import Signal
from .themes import latest_theme_leaderboard
from .trader import CycleReport, LiveClock, ManagedPosition, TraderState, run_cycle
from .universe import load_universe

log = logging.getLogger(__name__)


class ManualTradeRefused(Exception):
    """A manual arm / buy from the lookup page could not be done; ``message`` says why.
    ``can_override`` / ``can_arm`` / ``can_confirm`` tell the UI which alternative to offer."""

    def __init__(self, message: str, can_override: bool = False, can_arm: bool = False, can_confirm: bool = False, problems: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.can_override = can_override
        self.can_arm = can_arm
        self.can_confirm = can_confirm
        self.problems = problems or []

    def to_dict(self) -> dict:
        return {"message": self.message, "can_override": self.can_override, "can_arm": self.can_arm, "can_confirm": self.can_confirm, "problems": self.problems}


@dataclass
class SessionSettings:
    data: str = "auto"
    csv_dir: str = "data/csv"
    universe: str | None = None
    symbols: str | None = None
    config: Path | None = None
    sentiment_csv: Path | None = None
    themes: bool = True
    broker: str = "paper"
    state_dir: Path = Path("paper_state")
    charts: bool = True
    cache_dir: Path = Path("data/cache")
    overrides: dict = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return self.broker in LIVE_BROKERS


def build_config(config: Path | None, sentiment_csv: Path | None, themes: bool, overrides: dict | None = None) -> StrategyConfig:
    cfg = StrategyConfig.load(config)
    ov: dict = dict(overrides or {})
    if sentiment_csv is not None:
        ov.update({"sentiment.enabled": True, "sentiment.source": "csv", "sentiment.path": str(sentiment_csv)})
    if not themes:
        ov["themes.enabled"] = False
    return cfg.with_overrides(ov) if ov else cfg


def load_frames(
    data: str,
    csv_dir: str,
    universe: str | None,
    symbols: str | None,
    cfg: StrategyConfig,
    start: str | None,
    end: str | None,
    max_age_hours: float | None = None,
    cache_dir: Path = Path("data/cache"),
    stats: dict | None = None,
) -> tuple[dict[str, pd.DataFrame], StrategyConfig]:
    """Load price frames; returns the (possibly adjusted) config alongside them.

    Pass a dict as ``stats`` to receive the universe size and the provider's
    download outcome (cache hits, refreshed, stale, missing, errors).
    """
    data = resolve_data_kind(data)
    kwargs: dict = {"directory": csv_dir, "cache_dir": cache_dir}
    if max_age_hours is not None:
        kwargs["max_age_hours"] = max_age_hours
    provider: DataProvider = make_provider(data, **kwargs)
    syms = load_universe(universe, symbols)
    if cfg.regime.enabled:
        syms.extend(s for s in cfg.auxiliary_symbols if s not in syms)
    # Pull extra history so indicators are warm on the first tradeable day;
    # with no start, fetch enough for the 6-month momentum and theme ranks.
    anchor = pd.Timestamp(start) if start else pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    lookback_days = int(cfg.warmup_bars * 1.6) if start else int(cfg.warmup_bars * 3)
    fetch_start = str((anchor - pd.Timedelta(days=lookback_days)).date())
    frames = provider.load(syms, start=fetch_start, end=end)
    if stats is not None:
        stats["universe"] = len([s for s in syms if s not in cfg.auxiliary_symbols])
        stats["requested"] = len(syms)
        stats["loaded"] = len(frames)
        stats["provider"] = dict(getattr(provider, "stats", {}) or {})
        stats["source"] = data
    return frames, cfg


class TradingSession:
    def __init__(self, settings: SessionSettings):
        self.s = settings
        self.state_dir = Path(settings.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chart_dir = self.state_dir / "charts"
        self.report_path = self.state_dir / "last_report.json"
        self.full_report_path = self.state_dir / "last_full_report.json"  # the latest whole-universe scan
        self.insider_path = self.state_dir / "insider_scan.json"
        self.state_path = self.state_dir / "trader.json"
        self.health = ConnectionRegistry(self.state_dir / "connections.json")
        self.alerts = Alerter(self.health)
        # Saved settings (dashboard settings page) live next to the state.
        # ``settings.yaml`` takes over from ``--config`` once it exists; saved
        # credentials are exported to the environment so every module that
        # reads ``os.environ`` sees them.
        self.store = SettingsStore(self.state_dir)
        self.providers = ProviderStore(self.state_dir)
        self.requested_data = settings.data
        self.cfg: StrategyConfig = StrategyConfig()
        self.gatherer: ContextGatherer | None = None
        self._broker: Broker | None = None
        self._settings_signature: tuple = ()
        self.freshness: dict = {}
        self.reload_settings(force=True)

    def reload_settings(self, force: bool = False) -> bool:
        """Re-read ``settings.yaml`` / ``settings.env`` when they changed on disk.

        Returns True when anything was reloaded. Rebuilds the strategy config,
        the context gatherer and the resolved data source; a broker connection
        is re-opened lazily so new credentials take effect on the next call.
        """
        signature = self.store.signature() + (self._overrides_signature(),)
        if not force and signature == self._settings_signature:
            self.refresh_data_source()
            return False
        self._settings_signature = signature
        self.store.apply_env()
        self.providers.activate()
        # ``auto`` resolves here, so every report and status line names the
        # source that was actually used (and refuses simulated sources).
        self.refresh_data_source()
        config_path = self.store.yaml_path if self.store.yaml_path.exists() else self.s.config
        self.cfg = build_config(config_path, self.s.sentiment_csv, self.s.themes, self.s.overrides)
        # Learned knob values (qmag.learning) sit on top of the operator's
        # settings - but under explicit CLI overrides - while auto_apply is on.
        self.learned_overrides = {}
        if self.cfg.learning.enabled and self.cfg.learning.auto_apply:
            from .learning import load_overrides

            learned = {k: v for k, v in load_overrides(self.state_dir).items() if k not in self.s.overrides}
            if learned:
                try:
                    self.cfg = self.cfg.with_overrides(learned)
                    self.learned_overrides = learned
                except Exception as exc:  # pragma: no cover - a corrupt overrides file must not stop trading
                    log.warning("ignoring learning overrides: %s", exc)
        self.gatherer = (
            ContextGatherer(
                self.cfg, cache_path=self.state_dir / "context_cache.json", registry=self.health, uw_cache_path=self.state_dir / "uw_cache.json"
            )
            if self.cfg.context.enabled
            else None
        )
        if self.s.broker != "paper":
            self._broker = None
        return True

    def refresh_data_source(self) -> str:
        """Re-resolve ``--data auto`` (IBKR when the gateway answers, else Unusual
        Whales / Yahoo) and log when the effective source changes."""
        before = getattr(self.s, "data", None)
        self.s.data = resolve_data_kind(self.requested_data)
        if before and before != self.s.data:
            log.info("price data source: %s -> %s", before, self.s.data)
        return self.s.data

    def _overrides_signature(self) -> tuple | None:
        from .learning import OVERRIDES_FILE

        try:
            st = (self.state_dir / OVERRIDES_FILE).stat()
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None

    @property
    def config_source(self) -> str:
        """Where the strategy parameters currently come from (for the settings page)."""
        if self.store.yaml_path.exists():
            return str(self.store.yaml_path)
        return str(self.s.config) if self.s.config else "built-in defaults"

    # ------------------------------------------------------------------ #
    @property
    def broker(self) -> Broker:
        if self._broker is None:
            try:
                if self.s.broker == "paper":
                    self._broker = make_broker("paper", state_path=self.state_dir / "ledger.json", starting_cash=self.cfg.risk.starting_equity)
                else:
                    self._broker = make_broker(self.s.broker)
            except Exception as exc:
                self.health.record("broker", False, detail=f"{self.s.broker}: connect", error=f"{type(exc).__name__}: {exc}")
                raise
        return self._broker

    def account(self):
        """Read the broker account, recording the outcome. Raises if the broker is unreachable."""
        return self.health.record_result("broker", self.broker.account, detail=f"{self.s.broker}: account read")

    def load(self, asof: str | None = None, max_age_hours: float | None = None) -> tuple[dict[str, pd.DataFrame], StrategyConfig]:
        stats: dict = {}
        t0 = time.perf_counter()
        try:
            frames, cfg = load_frames(
                self.s.data, self.s.csv_dir, self.s.universe, self.s.symbols, self.cfg, None, asof,
                max_age_hours=max_age_hours, cache_dir=self.s.cache_dir, stats=stats,
            )
        except Exception as exc:
            self.health.record("price_data", False, detail=f"{self.s.data}: load failed", error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000, save=False)
            if isinstance(exc, FileNotFoundError):
                self.health.record("universe", False, error=str(exc), save=False)
            self.health.save()
            raise
        if asof:
            frames = {s: df.loc[:asof] for s, df in frames.items()}
            frames = {s: df for s, df in frames.items() if len(df)}
        self.cfg = cfg
        self._record_load(frames, stats, asof, (time.perf_counter() - t0) * 1000)
        return frames, cfg

    def _record_load(self, frames: dict[str, pd.DataFrame], stats: dict, asof: str | None, latency_ms: float) -> None:
        p = stats.get("provider", {})
        self.freshness = price_freshness(frames, asof)
        self.health.record("universe", True, detail=f"{stats.get('universe', 0)} tickers", items=stats.get("universe", 0), save=False)
        missing, stale, errors = p.get("missing", []), p.get("stale", []), p.get("errors", [])
        notes = [f"{len(frames)}/{stats.get('requested', len(frames))} symbols loaded"]
        if p.get("bulk_source"):
            notes.append(f"{p['bulk_symbols']} symbols exceed the Unusual Whales bulk threshold - sweep served by {p['bulk_source']}")
        if p.get("ibkr_error"):
            notes.append(f"IBKR unavailable ({p['ibkr_error']}) - load served by {p.get('source', 'yfinance')}")
        if p.get("fallback"):
            fb = p["fallback"]
            notes.append(f"{fb.get('loaded', 0)}/{fb.get('requested', 0)} symbols IB could not serve came from {fb.get('source', 'yfinance')}")
        if p.get("volume_multiplier") not in (None, 1, 1.0):
            notes.append(f"IB volume x{p['volume_multiplier']:g}")
        if p.get("volume_share_of_consolidated") and p["volume_share_of_consolidated"] < 0.9:
            notes.append(f"IB volume is lit-exchange only (~{p['volume_share_of_consolidated']:.0%} of consolidated)")
        if p.get("ibkr_errors"):
            top, n = next(iter(p["ibkr_errors"].items()))
            notes.append(f"IB said '{top}' for {n} symbols")
            log.info("IBKR error summary: %s", p["ibkr_errors"])
        if self.freshness.get("latest"):
            notes.append(f"latest bar {self.freshness['latest']}")
        if stale:
            notes.append(f"{len(stale)} served from stale cache (refresh failed)")
        if missing:
            notes.append(f"{len(missing)} returned no bars")
        if errors:
            notes.append(f"{len(errors)} download errors; last: {errors[-1]}")
        if self.freshness.get("stale"):
            notes.append(self.freshness["note"])
        problem = not frames or (self.freshness.get("stale") and not asof)
        degraded = bool(stale or errors or p.get("ibkr_error") or (missing and len(missing) > 0.1 * max(stats.get("requested", 1), 1)) or self.freshness.get("note"))
        self.health.record(
            "price_data", not problem, detail=f"{self.s.data}: " + "; ".join(notes), items=len(frames), latency_ms=latency_ms,
            degraded=degraded, error=(self.freshness.get("note") or "no price data loaded") if problem else None, save=False,
        )
        self.health.save()

    # ------------------------------------------------------------------ #
    @serialized
    def cycle(self, asof: str | None = None, max_age_hours: float | None = None, label: str = "cycle", now: datetime | None = None) -> CycleReport:
        """Refresh data, run one trading cycle, persist state, charts and the report.

        A live full scan (no ``asof``) carries the wall clock so a pass during
        the session knows today's bar is still forming: nothing is bought or
        cut on a partial bar whose volume cannot be judged."""
        t0 = time.perf_counter()
        try:
            if self.reload_settings():
                log.info("settings changed on disk; reloaded before the %s cycle", label)
            frames, cfg = self.load(asof, max_age_hours)
            if not frames:
                raise RuntimeError("No price data loaded; check the provider, universe or network access")
            block = None
            if self.freshness.get("stale") and not asof:
                block = "price data stale: " + self.freshness["note"]
            self.account()  # fail loudly (and record it) before touching any state
            state = TraderState.load(self.state_path)
            state._persistence_path = self.state_path
            clock = now if (now is not None or asof) else datetime.now(NY)
            report = run_cycle(
                frames, self.broker, cfg, state, asof=pd.Timestamp(asof) if asof else None, gatherer=self.gatherer, block_new_entries=block,
                label=label, live=LiveClock(now=clock), entry_guard=lambda: self.halted(), halted=self.halted(),
            )
            state.save(self.state_path)
            self.health.record_llm(report.plans + report.rejected)
            self._record_regime(report)
            self.write_report(report, frames, cfg, label=label)
        except Exception as exc:
            self.health.record("cycle", False, detail=label, error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000)
            self.alerts.failure(f"{label} cycle", f"{type(exc).__name__}: {exc}", key=f"cycle-fail:{label}:{datetime.now(NY).date()}")
            raise
        self.health.record("cycle", True, detail=f"{label} as of {report.asof}", latency_ms=(time.perf_counter() - t0) * 1000, degraded=bool(report.data_gaps))
        self.alerts.report(report, label)
        return report

    # ------------------------------------------------------------------ #
    # Tiered schedule: focused passes, screens, the weekly insider scan
    # ------------------------------------------------------------------ #
    def focus_scope(self, extra: list[str] | None = None) -> list[str]:
        """Arming list + open positions + resting entries + screener hits (no aux symbols)."""
        state = self.state()
        scope = set(state.arming) | set(state.managed) | set(state.pending) | {s.upper() for s in (extra or [])}
        return sorted(s for s in scope if s not in self.cfg.auxiliary_symbols)

    @serialized
    def focused_cycle(
        self,
        label: str = "focused",
        extra_symbols: list[str] | None = None,
        screen_hits: dict[str, dict] | None = None,
        asof: str | None = None,
        now: datetime | None = None,
        max_age_hours: float | None = None,
    ) -> CycleReport | None:
        """The intraday pass: refresh only the names that matter, project
        today's partial volume to a full-session pace, and run the trader in
        focused mode. Returns ``None`` (after recording why) when there is
        nothing armed, held or pending to look at."""
        t0 = time.perf_counter()
        try:
            if self.reload_settings():
                log.info("settings changed on disk; reloaded before the %s pass", label)
            cfg = self.cfg
            scope = self.focus_scope(extra_symbols)
            if not scope:
                self.health.record("cycle", True, detail=f"{label}: nothing armed, held or pending - no data requested", latency_ms=(time.perf_counter() - t0) * 1000)
                return None
            syms = list(scope) + [s for s in cfg.auxiliary_symbols if cfg.regime.enabled]
            if max_age_hours is None:
                max_age_hours = max(cfg.schedule.focused_interval_minutes / 60.0 * 0.8, 0.02)
            stats: dict = {}
            t1 = time.perf_counter()
            try:
                frames, cfg = load_frames(
                    self.s.data, self.s.csv_dir, None, ",".join(syms), cfg, None, asof, max_age_hours=max_age_hours, cache_dir=self.s.cache_dir, stats=stats,
                )
            except Exception as exc:
                self.health.record("price_data", False, detail=f"{self.s.data}: {label} load failed", error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t1) * 1000)
                raise
            if asof:
                frames = {s: df.loc[:asof] for s, df in frames.items()}
                frames = {s: df for s, df in frames.items() if len(df)}
            if not frames:
                raise RuntimeError(f"No price data loaded for the {label} pass ({len(scope)} symbols requested)")
            self.cfg = cfg
            self._record_load(frames, stats, asof, (time.perf_counter() - t1) * 1000)
            pre_actions: list[str] = [f"FOCUS {label}: {len(scope)} names (arming list + positions + pending" + (" + screen hits" if extra_symbols else "") + ")"]
            block = None
            if self.freshness.get("stale") and not asof:
                block = "price data stale: " + self.freshness["note"]
            # ``now`` given explicitly = "judge the bars as of this wall-clock
            # time" (also for replays); otherwise a replay treats bars as complete.
            clock = now if (now is not None or asof) else datetime.now(NY)
            note = None
            if cfg.schedule.volume_pace and clock is not None:
                from .pace import project_volume

                frames, note = project_volume(frames, clock, min_fraction=cfg.schedule.pace_min_session_fraction)
                if note.get("applied"):
                    pre_actions.append(f"PACE today's volume x{note['multiplier']:.2f} ({note['fraction']:.0%} of the session done) for {note['symbols']} names (projection, not a print)")
                elif note.get("reason"):
                    pre_actions.append(f"PACE not applied: {note['reason']}")
            live = LiveClock(now=clock, pace=note)
            regime = self._focused_regime(frames, cfg)
            self.account()
            state = TraderState.load(self.state_path)
            state._persistence_path = self.state_path
            report = run_cycle(
                frames, self.broker, cfg, state, asof=pd.Timestamp(asof) if asof else None, gatherer=self.gatherer, block_new_entries=block,
                scan_symbols=set(scope), regime=regime, full_scan=False, pre_actions=pre_actions, screen_hits=screen_hits, label=label, live=live,
                entry_guard=lambda: self.halted(), halted=self.halted(),
            )
            state.save(self.state_path)
            self.health.record_llm(report.plans + report.rejected)
            self.write_report(report, frames, cfg, label=label)
        except Exception as exc:
            self.health.record("cycle", False, detail=label, error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000)
            self.alerts.failure(f"{label} pass", f"{type(exc).__name__}: {exc}", key=f"cycle-fail:{label}:{datetime.now(NY).date()}")
            raise
        self.health.record("cycle", True, detail=f"{label} pass over {len(report.scope)} names as of {report.asof}", latency_ms=(time.perf_counter() - t0) * 1000, degraded=bool(report.data_gaps))
        self.alerts.report(report, label)
        return report

    def _focused_regime(self, frames: dict[str, pd.DataFrame], cfg: StrategyConfig) -> tuple[bool, str, bool]:
        """Regime for a focused pass: fresh benchmark / VIX gates on the aux bars
        AND the breadth verdict of the latest full scan (breadth needs the whole
        universe). No recent full scan -> unknown -> risk-off (fail closed)."""
        if not cfg.regime.enabled:
            return True, "regime filter disabled in config", True
        aux = {s: df for s, df in frames.items() if s in cfg.auxiliary_symbols}
        if not aux:
            return False, "REGIME UNKNOWN - benchmark / VIX bars not loaded (treated as risk-off)", False
        nb_cfg = cfg.with_overrides({"regime.breadth_enabled": False}) if cfg.regime.breadth_enabled else cfg
        snap = regime_snapshot(aux, nb_cfg)
        note, ok, known = snap.describe(nb_cfg), snap.ok, snap.known
        if not cfg.regime.breadth_enabled:
            return ok, note, known
        prior = self.last_full_report() or {}
        latest = max(df.index[-1] for df in frames.values()).date()
        if prior.get("asof") and pd.Timestamp(prior["asof"]).date() >= latest - pd.Timedelta(days=4):
            p_ok, p_known = bool(prior.get("regime_ok", False)), bool(prior.get("regime_known", True))
            note += f"; breadth from the {prior.get('label', 'full')} scan as of {prior['asof']}: {'ok' if p_ok else 'risk-off'}" if p_known else "; breadth unknown in the last full scan"
            return ok and p_ok, note, known and p_known
        return False, note + "; breadth unknown - no full scan in the last 4 sessions (treated as risk-off)", False

    @serialized
    def screen_cycle(self, kind: str, now: datetime | None = None) -> dict:
        """Pre-market gap screen or intraday movers sweep.

        ``premarket`` arms screener hits (no detection: there are no bars for
        today yet) so the first focused passes look at them; ``movers`` runs a
        focused pass with the hits added to the scope. Returns the screen
        result (with any data gap) as a dict.
        """
        from .screener import run_screen

        t0 = time.perf_counter()
        self.reload_settings()
        cfg = self.cfg
        res = run_screen(kind, cfg, uw_cache_path=self.state_dir / "uw_cache.json")
        out = res.to_dict()
        if res.unavailable:
            self.health.record("screener", False, detail=f"{kind} screen", error=res.error or "no screener source configured", latency_ms=(time.perf_counter() - t0) * 1000)
            out["note"] = f"{kind} screen NOT RUN: {res.error}"
            self._append_screen_note(kind, out)
            return out
        self.health.record(
            "screener", res.error is None, detail=f"{kind} screen via {res.source}: {len(res.hits)} hits from {res.considered} rows", items=len(res.hits),
            error=res.error, latency_ms=(time.perf_counter() - t0) * 1000, degraded=bool(res.error),
        )
        hits = {h["symbol"]: h for h in res.hits}
        if kind == "premarket":
            state = TraderState.load(self.state_path)
            state._persistence_path = self.state_path
            added = []
            for sym, h in hits.items():
                if sym in state.managed or sym in cfg.auxiliary_symbols:
                    continue
                rec = state.arming.get(sym)
                if rec is None:
                    state.arming[sym] = {
                        "symbol": sym, "setup": "gap", "pivot": None, "entry": None, "stop": None, "score": 0.0,
                        "distance_pct": None, "triggered": False, "source": "premarket", "armed_on": pd.Timestamp.now("America/New_York").strftime("%Y-%m-%d"),
                        "last_seen": pd.Timestamp.now("America/New_York").strftime("%Y-%m-%d"), "screen": h,
                    }
                    added.append(sym)
                else:
                    rec["screen"] = h
            state.save(self.state_path)
            out["armed"] = added
            out["note"] = f"pre-market gaps >= {cfg.schedule.premarket_min_gap_pct:.0%}: {len(hits)} hits, {len(added)} newly armed for the focused passes"
            self._append_screen_note(kind, out)
            return out
        report = self.focused_cycle(label="movers", extra_symbols=list(hits), screen_hits=hits, now=now)
        out["note"] = f"movers >= {cfg.schedule.movers_min_change_pct:.0%} on >= {cfg.schedule.movers_min_rvol:g}x volume: {len(hits)} hits" + (f"; focused pass over {len(report.scope)} names" if report else "")
        out["report_asof"] = report.asof if report else None
        return out

    def _append_screen_note(self, kind: str, out: dict) -> None:
        """Keep the last screen outcome visible on the dashboard even when no cycle ran."""
        path = self.state_dir / "screens.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError:
            data = {}
        data[kind] = {**out, "at": pd.Timestamp.now("UTC").isoformat()}
        atomic_json(path, data)

    @serialized
    def insider_scan(self, ai: bool | None = None, now: datetime | None = None) -> dict:
        """The Saturday unusual-options review (see ``qmag.insider_scan``); writes ``insider_scan.json``."""
        from .insider_scan import load_report, run_weekly_scan

        self.reload_settings()
        previous = load_report(self.insider_path)

        def bars(symbols: list[str], start: str) -> dict:
            # The desk's own daily-bar provider: the scan uses it for the
            # share-price backdrop of each candidate and to measure how
            # earlier weeks' flags actually played out.
            provider = make_provider(resolve_data_kind(self.s.data), directory=self.s.csv_dir, cache_dir=self.s.cache_dir)
            return provider.load(symbols, start=start)

        report = run_weekly_scan(
            self.cfg, now=now, uw_cache_path=self.state_dir / "uw_cache.json", ai=ai, previous=previous, registry=self.health, bars=bars,
        )
        atomic_json(self.insider_path, report)
        return report

    def last_insider_scan(self) -> dict | None:
        from .insider_scan import load_report

        return load_report(self.insider_path)

    @serialized
    def learn(self, apply: bool | None = None, ai: bool | None = None, now: datetime | None = None) -> dict:
        """The learning review (see ``qmag.learning.review``): post-mortems,
        lessons, filter / gate scores and bounded knob adjustments. Writes
        ``learning_report.json`` (and ``learning_overrides.yaml`` when applied);
        the next cycle picks the new values up through ``reload_settings``."""
        from .learning import review

        t0 = time.perf_counter()
        self.reload_settings()
        state = TraderState.load(self.state_path)
        state._persistence_path = self.state_path
        try:
            report = review(state, self.cfg, self.state_dir, now=now, apply=apply, ai=ai, registry=self.health)
        except Exception as exc:
            self.health.record("learning", False, detail="review", error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000)
            raise
        state.save(self.state_path)  # post-mortems (rule-based and AI) are stored on the journal entries
        return report

    def last_learning_report(self) -> dict | None:
        from .learning import load_report

        return load_report(self.state_dir)

    @serialized
    def reset_learning(self) -> bool:
        """Drop the learned knob values; the operator's settings apply again from the next cycle."""
        from .learning import reset_overrides

        removed = reset_overrides(self.state_dir)
        self.reload_settings()
        self.health.record("learning", True, detail="learned adjustments reset by the operator")
        return removed

    def last_full_report(self) -> dict | None:
        if self.full_report_path.exists():
            return json.loads(self.full_report_path.read_text())
        prior = self.last_report()  # reports written before the tiered schedule were all full scans
        return prior if prior and prior.get("scan", "full") == "full" else None

    def _record_regime(self, report: CycleReport) -> None:
        if not self.cfg.regime.enabled:
            return
        if report.regime_known:
            self.health.record("regime_data", True, detail=report.regime_note, save=False)
        else:
            gap = next((g for g in report.data_gaps if g.startswith("regime unknown")), report.regime_note)
            self.health.record("regime_data", False, detail=report.regime_note, error=gap, save=False)
        self.health.save()

    def write_report(self, report: CycleReport, frames: dict[str, pd.DataFrame], cfg: StrategyConfig, label: str = "cycle") -> dict:
        payload = report.to_dict()
        payload["label"] = label
        payload["generated_at"] = pd.Timestamp.now("UTC").isoformat()
        payload["universe_size"] = len([s for s in frames if s not in cfg.auxiliary_symbols])
        payload["data_source"] = self.s.data
        payload["freshness"] = self.freshness
        payload["config"] = cfg.to_dict()
        payload["charts"] = {}
        if report.scan != "full":
            # A focused pass only loaded a handful of names: theme ranks and the
            # universe size come from the latest full scan, labelled as such.
            full = self.last_full_report() or {}
            payload["universe_size"] = full.get("universe_size", payload["universe_size"])
            payload["themes"] = full.get("themes", [])
            payload["full_scan_asof"] = full.get("asof")
            payload["full_scan_label"] = full.get("label")
        elif cfg.themes.enabled:
            try:
                enriched = prepare_data(frames, cfg)
                board = latest_theme_leaderboard(enriched, cfg)
                payload["themes"] = json.loads(board.to_json(orient="records")) if not board.empty else []
            except Exception as exc:  # pragma: no cover - report must never fail on a side table
                log.warning("theme leaderboard failed: %s", exc)
                payload["themes"] = []
                payload["data_gaps"].append(f"theme leaderboard failed: {type(exc).__name__}: {exc}")
        if self.s.charts:
            try:
                payload["charts"] = self.render_charts(report, frames, cfg)
            except Exception as exc:  # pragma: no cover
                log.warning("chart rendering failed: %s", exc)
                payload["data_gaps"].append(f"chart rendering failed: {type(exc).__name__}: {exc}")
        text = json.dumps(payload, indent=2, default=str)
        atomic_text(self.report_path, text)
        if report.scan == "full":
            atomic_text(self.full_report_path, text)
        # The account snapshot (accounts page, other desks) rides on the bars
        # this cycle loaded; it must never fail the report.
        try:
            self.account_snapshot(frames)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("account snapshot failed: %s", exc)
        return payload

    def account_snapshot(self, frames: dict[str, pd.DataFrame] | None = None, fetch_missing: bool = True) -> dict:
        """Write ``account.json``: the broker's equity, cash and every holding
        marked at the latest real bar (see ``qmag.accounts``)."""
        from .accounts import write_snapshot

        return write_snapshot(self, frames=frames, fetch_missing=fetch_missing)

    def accounts(self, refresh: bool = False) -> dict:
        """Every account this desk and the desks in ``QMAG_DESKS`` drive, with per-currency totals."""
        from .accounts import desk_overview

        return desk_overview(self, refresh_local=refresh)

    def render_charts(self, report: CycleReport, frames: dict[str, pd.DataFrame], cfg: StrategyConfig) -> dict[str, str]:
        """Charts for every plan (triggered or watch) and every open position."""
        enriched = prepare_data(frames, cfg)
        out: dict[str, str] = {}
        signals: dict[str, Signal] = {s.symbol: s for s in report.watchlist + report.triggered}
        for plan in report.plans + report.rejected[:12]:
            sig = signals.get(plan.symbol)
            if sig is None or plan.symbol not in enriched:
                continue
            note = plan.summary() if plan.ok else f"NOT TAKEN: failed {', '.join(plan.failed_checks)}"
            path = chart_signal(enriched[plan.symbol], sig, self.chart_dir, target=plan.partial_target, shares=plan.shares or None, note=note)
            out[plan.symbol] = str(path)
        for pos in report.open_positions:
            if pos.symbol not in enriched or pos.symbol in out:
                continue
            levels = ChartLevels(
                entry=pos.entry_price, stop=pos.stop, target=None if pos.partial_done else pos.target, pivot=pos.pivot,
                shares=pos.remaining, theme=pos.theme, setup=pos.setup,
                note=f"opened {pos.entry_date}; initial stop {pos.initial_stop:.2f}; {'partial taken, trailing ' + str(cfg.management.trail_ma) + 'd MA' if pos.partial_done else 'partial pending'}",
            )
            path = render_chart(enriched[pos.symbol], pos.symbol, levels, self.chart_dir / f"{pos.symbol}_open.png")
            out[pos.symbol] = str(path)
        return out

    # ------------------------------------------------------------------ #
    def connections(self) -> dict:
        """Status of every connection (see ``health.describe_connections``)."""
        return describe_connections(self.s, self.cfg, self.state_dir, self.health)

    def current_regime(self, frames: dict[str, pd.DataFrame] | None = None) -> tuple[bool, str, bool]:
        """(regime_ok, note, known) for on-demand lookups.

        Prefers the last cycle report when it is from the latest session;
        otherwise evaluates the benchmark / VIX gates on ``frames`` (breadth
        needs the whole universe, so it is reported as unavailable rather than
        computed from one symbol). Nothing is assumed when no input exists.
        """
        if not self.cfg.regime.enabled:
            return True, "regime filter disabled in config", True
        prior = self.last_report() or {}
        if prior.get("regime_note") and prior.get("asof") and frames:
            latest = max(df.index[-1] for df in frames.values()).date()
            if pd.Timestamp(prior["asof"]).date() >= latest - pd.Timedelta(days=4):
                return bool(prior.get("regime_ok", False)), f"{prior['regime_note']} (from the {prior.get('label', 'last')} cycle as of {prior['asof']})", bool(prior.get("regime_known", True))
        aux = {s: df for s, df in (frames or {}).items() if s in self.cfg.auxiliary_symbols}
        if not aux:
            return False, "REGIME UNKNOWN - no cycle report and no benchmark bars loaded (treated as risk-off)", False
        cfg = self.cfg.with_overrides({"regime.breadth_enabled": False}) if self.cfg.regime.breadth_enabled else self.cfg
        snap = regime_snapshot(aux, cfg)
        note = snap.describe(cfg)
        if self.cfg.regime.breadth_enabled:
            note += "; breadth not evaluated (needs a full cycle)"
        return snap.ok, note, snap.known

    def analyze_symbol(self, symbol: str, max_age_hours: float = 0.25) -> dict:
        """On-demand desk check for one ticker: setup status, sized plan, context, rationale, chart.

        Used by the dashboard's symbol lookup. Nothing is traded or persisted
        except the rendered chart. Every input that could not be sourced is
        listed in ``data_gaps`` instead of being substituted.
        """
        return self._analyze_symbol(symbol, max_age_hours)[0]

    def _analyze_symbol(self, symbol: str, max_age_hours: float = 0.25) -> tuple[dict, object | None, pd.DataFrame, object | None]:
        """``analyze_symbol`` plus the live objects (signal, enriched frame, TradePlan) the manual trade path needs."""
        from .backtest import default_detectors
        from .plan import apply_committee, apply_reviewer, build_plan
        from .setups import BreakoutDetector, momentum_ok

        symbol = symbol.upper()
        cfg = self.cfg
        syms = symbol + ("," + ",".join(cfg.auxiliary_symbols) if cfg.regime.enabled else "")
        stats: dict = {}
        frames, cfg = load_frames(self.s.data, self.s.csv_dir, None, syms, cfg, None, None, max_age_hours=max_age_hours, cache_dir=self.s.cache_dir, stats=stats)
        if symbol not in frames:
            errors = stats.get("provider", {}).get("errors", [])
            why = f" ({errors[-1]})" if errors else ""
            self.health.record("price_data", False, detail=f"{self.s.data}: lookup {symbol}", error=f"no bars for {symbol}{why}")
            raise KeyError(f"No price data for {symbol} from {self.s.data}{why}")
        gaps: list[str] = []
        prov = stats.get("provider", {})
        if symbol in prov.get("stale", []):
            gaps.append(f"{symbol} bars served from a cache that could not be refreshed")
        enriched = prepare_data(frames, cfg)
        df = enriched[symbol]
        last = df.iloc[-1]
        fresh = price_freshness({symbol: frames[symbol]})
        if fresh.get("stale"):
            gaps.append("price data stale: " + fresh["note"])
        regime_ok, regime_note, regime_known = self.current_regime(frames)
        if not regime_known:
            gaps.append("market regime unknown: " + regime_note)
        try:
            acct = self.account()
            equity, cash, equity_known = acct.equity, acct.cash, True
        except Exception as exc:
            equity = cash = 0.0
            equity_known = False
            gaps.append(f"broker account unavailable ({type(exc).__name__}: {exc}); no equity assumed, plan not sized")

        sig = None
        for det in default_detectors():
            sig = det.detect_last(symbol, df, cfg) or sig
        watch = BreakoutDetector().watchlist(symbol, df, cfg) if sig is None else None
        status = "triggered" if sig is not None else "watch" if watch is not None else "none"
        sig = sig or watch

        context = self.gatherer.one(symbol) if self.gatherer is not None else None
        if context is not None:
            self.health.record_sources([context])
        out: dict = {
            "symbol": symbol,
            "asof": str(df.index[-1].date()),
            "status": status,
            "last_close": float(last["close"]),
            "momentum_leader": bool(momentum_ok(last, cfg)),
            "facts": {
                k: (None if pd.isna(last.get(k, float("nan"))) else float(last[k]))
                for k in ("gain_1m", "gain_3m", "gain_6m", "adr_pct", "dollar_vol_20", "sma_10", "sma_20", "sma_50", "rvol_20", "gap_pct")
                if k in df.columns
            },
            "theme": last.get("theme") if isinstance(last.get("theme"), str) else None,
            "theme_pct": None if pd.isna(last.get("theme_pct", float("nan"))) else float(last["theme_pct"]),
            "context": context.to_dict() if context is not None else None,
            "regime_ok": regime_ok,
            "regime_known": regime_known,
            "regime_note": regime_note,
            "equity_known": equity_known,
            "data_gaps": gaps,
            "plan": None,
            "chart": None,
            "chart_note": None,
        }
        self.chart_dir.mkdir(parents=True, exist_ok=True)
        plan = None
        if sig is not None:
            positions = self.open_positions()
            exposure = sum(p.remaining * p.entry_price for p in positions)
            plan = build_plan(
                sig, df.iloc[-2] if status == "triggered" and len(df) > 1 else last, cfg, equity, exposure, cash, regime_ok,
                entry_override=None, context_row=None, context=context, regime_note=regime_note,
                context_expected=self.gatherer is not None, equity_known=equity_known,
            )
            if fresh.get("stale"):
                plan.checks["price_data_fresh"] = False
            plan.data_gaps = list(dict.fromkeys(gaps + plan.data_gaps))
            plan = apply_committee(plan, cfg)
            if cfg.reviewer.enabled:
                state = self.state()
                portfolio = {
                    "equity": equity if equity_known else None,
                    "cash": cash if equity_known else None,
                    "open_positions": len(positions),
                    "pending_entries": len(state.pending),
                    "max_positions": cfg.risk.max_positions,
                    "held_symbols": sorted(p.symbol for p in positions),
                    "broker": self.s.broker,
                }
                plan = apply_reviewer(
                    plan, cfg, regime_ok, regime_note, portfolio,
                    entry_mode="market now" if status == "triggered" else "buy-stop at pivot for the next session",
                    review_rejected=True,
                )
                self.health.record_llm([plan])
            out["plan"] = plan.to_dict()
            note = plan.summary() if plan.ok else f"NOT TAKEN: failed {', '.join(plan.failed_checks)}"
            if status == "watch":
                note = "READY: " + note
            path = chart_signal(df, sig, self.chart_dir, target=plan.partial_target, shares=plan.shares or None, note=note)
            out["chart"] = str(path)
        else:
            adr = last.get("adr_dollar", float("nan"))
            entry = float(last["close"])
            if adr is None or pd.isna(adr):
                # Not enough history for a real ADR: draw the chart without a
                # trade plan rather than inventing a stop from a guessed range.
                out["chart_note"] = "No setup on the latest bar. ADR could not be computed from the available history, so no illustrative stop / target is drawn."
                gaps.append("ADR unavailable (too little history): no illustrative levels drawn")
                path = render_chart(df, symbol, ChartLevels(entry=entry, stop=None, setup="no setup", note="No setup; ADR unknown, so no stop / target is drawn."), self.chart_dir / f"{symbol}_lookup.png")
            else:
                stop = entry - cfg.management.stop_adr_mult * float(adr)
                out["chart_note"] = (
                    f"No breakout flag or episodic pivot on the latest bar. Levels are illustrative only: last close {entry:.2f}, "
                    f"stop {cfg.management.stop_adr_mult:g} x ADR (${float(adr):.2f}) below it, target +{cfg.management.partial_target_r or 2}R. They are not a trade plan."
                )
                levels = ChartLevels(
                    entry=entry, stop=stop, target=entry + (cfg.management.partial_target_r or 2) * (entry - stop), setup="no setup",
                    note="No setup on the latest bar; levels are illustrative (last close, 1 ADR stop), not a plan.",
                )
                path = render_chart(df, symbol, levels, self.chart_dir / f"{symbol}_lookup.png")
            out["chart"] = str(path)
        return out, sig, df, plan

    # ------------------------------------------------------------------ #
    @serialized
    def manual_trade(self, symbol: str, action: str, confirm_live: bool = False, now: datetime | None = None) -> dict:
        """Act on a lookup: ``arm`` the setup for the focused passes, ``buy`` it at
        market with the plan's stop and target when it has triggered and every
        check and confirmation gate passes, or ``override`` (buy despite failed
        checks / gates - recorded as such so the review can score it).

        The plan is rebuilt from fresh data at click time; nothing is placed
        from numbers the page showed earlier. A position opened here is
        adopted by the trader and managed exactly like an automated one.
        Raises ``ManualTradeRefused`` with the reason when it cannot be done.
        """
        from dataclasses import asdict

        from .trader import (
            CycleReport,
            PendingPlan,
            _adopt,
            _arm_record,
            _await_fill,
            confirmation_gates,
            ensure_exit_orders,
            entry_features,
            portfolio_gate,
            portfolio_heat,
        )

        if action not in ("arm", "buy", "override"):
            raise ManualTradeRefused(f"unknown action '{action}'")
        symbol = symbol.upper().strip()
        if self.s.live and action != "arm" and not confirm_live:
            raise ManualTradeRefused("LIVE account: confirm the order explicitly before it is sent", can_confirm=True)
        halted = self.halted()
        if halted and action != "arm":
            raise ManualTradeRefused(f"{halted}. Resume trading on the connections page (or `qmag resume`) first", can_arm=True)
        self.reload_settings()
        out, sig, df, plan = self._analyze_symbol(symbol, max_age_hours=0.0)
        cfg = self.cfg
        state = self.state()
        if sig is None or plan is None:
            raise ManualTradeRefused(f"{symbol} has no breakout flag or episodic pivot on the latest bar - there is nothing to arm or buy")
        if symbol in state.managed:
            raise ManualTradeRefused(f"{symbol} is already an open position")
        if symbol in state.pending:
            raise ManualTradeRefused(f"{symbol} already has an entry order resting")
        asof = out["asof"]
        clock = now or datetime.now(NY)
        live = LiveClock(now=clock)
        status = out["status"]
        if action == "arm":
            state.arming[symbol] = _arm_record(sig, "manual", asof, extra={"manual": True, "armed_at": pd.Timestamp(clock).isoformat(timespec="minutes")}, triggered=status == "triggered")
            state.save(self.state_path)
            return {
                "ok": True, "action": "arm", "symbol": symbol, "asof": asof,
                "message": f"{symbol} armed: the focused passes will buy at market once {sig.pivot:.2f} breaks and holds on volume" if cfg.entry.mode != "resting" else f"{symbol} armed: the next scan will park a buy-stop at {sig.entry:.2f}",
            }
        if status != "triggered":
            raise ManualTradeRefused(f"{symbol} has not cleared its pivot ({sig.pivot:.2f}; last {out['last_close']:.2f}). Buying below the pivot is not the method - arm it instead", can_arm=True)
        problems: list[str] = []
        if not plan.ok:
            problems += [f"check failed: {c}" for c in plan.failed_checks]
        gates = confirmation_gates(sig, df.iloc[-1], cfg, live) if cfg.entry.mode in ("confirmed", "hybrid") else []
        problems += [f"gate: {g}" for g in gates]
        if problems and action == "buy":
            raise ManualTradeRefused("not confirmed: " + "; ".join(problems), can_override=True, problems=problems)
        last_close = float(df.iloc[-1]["close"])
        if float(plan.stop) >= last_close:
            # Not overridable: a stop at or above the market would fire the moment it is placed.
            raise ManualTradeRefused(f"the plan's stop {plan.stop:.2f} is at or above the last price {last_close:.2f} - a market buy here would be stopped out immediately. Wait for price to reclaim the pivot or arm it", can_arm=True, problems=problems)
        if plan.shares <= 0:
            raise ManualTradeRefused("the plan sizes to 0 shares (risk budget, position cap or unknown equity) - nothing to buy")
        if len(state.managed) + len(state.pending) >= cfg.risk.max_positions:
            raise ManualTradeRefused(f"portfolio full: {cfg.risk.max_positions} positions / pending entries already")
        broker = self.broker
        # Portfolio risk limits are not overridable: heat cap, daily loss circuit-breaker, theme concentration.
        equity = float(broker.account().equity)
        day = state.day_equity if state.day_equity and state.day_equity.get("date") == asof else None
        day_pnl = (equity - float(day["equity"])) / float(day["equity"]) if day and float(day.get("equity") or 0) > 0 else None
        limits = portfolio_gate(plan, state, cfg, portfolio_heat(state, {symbol: df}, equity), day_pnl, equity)
        if limits:
            raise ManualTradeRefused("portfolio risk limit: " + "; ".join(limits), problems=problems)
        if symbol in broker.positions():
            raise ManualTradeRefused(f"{symbol} is already held at the broker")
        if hasattr(broker, "mark"):
            broker.mark({symbol: df.iloc[-1]})  # the paper ledger fills market orders at the latest real close
        features = entry_features(sig, plan, df.iloc[-1], "market_manual", "manual", "lookup", live, out["regime_ok"], out["regime_note"], 1.0)
        features.update(manual=True, override=bool(problems), override_reasons=problems)
        if self.halted():
            raise ManualTradeRefused(self.halted())
        order = broker.market_buy(symbol, plan.shares, tag=f"manual:{symbol}")
        pending = PendingPlan(
            symbol, sig.setup, asof, float(plan.entry), float(plan.stop), int(plan.shares), order.id, entry_kind="market",
            target=plan.partial_target, partial_qty=plan.partial_qty, theme=plan.theme, plan=plan.to_dict(), features=features,
        )
        report = CycleReport(asof=asof, regime_ok=bool(out["regime_ok"]), equity=0.0, broker=getattr(broker, "name", self.s.broker), scan="manual", entry_mode=cfg.entry.mode)
        state.pending[symbol] = asdict(pending)
        state.checkpoint()
        filled = _await_fill(broker, order, symbol)
        result: dict = {
            "ok": True, "action": action, "symbol": symbol, "asof": asof, "order_id": order.id, "shares": int(plan.shares), "stop": float(plan.stop),
            "target": plan.partial_target, "override": bool(problems), "override_reasons": problems, "live": bool(self.s.live), "filled": None,
        }
        if filled is None:
            state.pending[symbol] = asdict(pending)
            result["message"] = f"market buy {plan.shares} {symbol} sent (order {order.id}); not filled yet - the next cycle adopts it and places the stop {plan.stop:.2f}"
        else:
            qty, avg = filled
            pos = _adopt(state, pending, qty, avg, asof, cfg)
            ensure_exit_orders(broker, pos, report)
            result["filled"] = {"qty": qty, "avg_price": round(avg, 4)}
            result["stop"], result["target"] = float(pos.stop), pos.target  # re-anchored to the actual fill
            tgt = f", target {pos.target:.2f}" if pos.target else ""
            result["message"] = f"bought {qty} {symbol} @ {avg:.2f}; stop {pos.stop:.2f}{tgt} placed - now managed by the trader" + (" (OVERRIDE)" if problems else "")
        state.arming.pop(symbol, None)
        state.save(self.state_path)
        self.health.record("cycle", True, detail=f"manual {action} {symbol}: {result['message'][:120]}", save=True)
        self.alerts.send(f"Manual {action}: {symbol}", result["message"], level="warn" if problems else "info")
        log.info("manual %s %s: %s", action, symbol, result["message"])
        return result

    # ------------------------------------------------------------------ #
    # Kill switch
    # ------------------------------------------------------------------ #
    def halted(self) -> str | None:
        """The kill-switch reason when trading is halted, else None."""
        return halt_reason(self.state_dir)

    def halt_state(self) -> dict | None:
        return halt_status(self.state_dir)

    def halt(self, on: bool, reason: str = "", by: str = "cli", flatten: bool = False, now: datetime | None = None) -> dict:
        # Persist before acquiring the writer lease or contacting the broker.
        # An in-flight cycle rechecks this flag immediately before submitting.
        if on:
            previous = halt_status(self.state_dir)
            reason = reason.strip() or (previous or {}).get("reason", "")
            set_halt(self.state_dir, True, reason, by=by, now=now)
        return self._halt_locked(on, reason, by, flatten, now)

    @serialized
    def _halt_locked(self, on: bool, reason: str = "", by: str = "cli", flatten: bool = False, now: datetime | None = None) -> dict:
        """Throw (``on=True``) or clear the kill switch. With ``flatten`` every
        managed position is sold at market and every order cancelled first;
        the closes are journaled with reason ``halt`` so the review sees them.
        Without ``flatten`` open positions stay protected by their stops and
        keep being managed; only new entries stop."""
        result: dict = {"on": on, "flattened": [], "cancelled": 0, "message": ""}
        if on:
            state = self.state()
            prev = halt_status(self.state_dir)
            if prev and not reason.strip():
                reason = prev["reason"]  # flattening an already-halted desk keeps the original reason
            # "No new entries" includes the buy-stops already resting at the
            # broker: a halt cancels them now, not at the next cycle, so
            # nothing can fire in between. (The broker is only contacted when
            # there is something to cancel or to flatten.)
            for sym in list(state.pending):
                self.broker.cancel_orders(sym)
                state.pending.pop(sym)
                result["cancelled"] += 1
            if flatten:
                broker = self.broker
                asof = str(pd.Timestamp(now or datetime.now(NY)).date())
                held = broker.positions()
                for sym, rec in list(state.managed.items()):
                    pos = ManagedPosition(**rec)
                    broker.cancel_orders(sym)
                    qty = held[sym].qty if sym in held else pos.remaining
                    from .trader import submit_exit
                    rep = CycleReport(asof=asof, regime_ok=False, equity=0.0)
                    if qty > 0 and submit_exit(broker, pos, qty, "halt", asof, state, rep):
                        closed = state.closed[-1]
                        result["flattened"].append({"symbol": sym, "qty": qty, "price": closed["exit_price"], "r_multiple": closed.get("r_multiple")})
                    else:
                        result.setdefault("pending", []).append(sym)
                    state.save(self.state_path)
            state.save(self.state_path)
            st = set_halt(self.state_dir, True, reason, by=by, flattened=flatten and not state.managed, now=now)
            result["status"] = st
            if result.get("pending"):
                result["execution_note"] = "Flatten requested; pending executions require broker reconciliation. Positions remain tracked."
            n = len(result["flattened"])
            c = result["cancelled"]
            result["message"] = f"trading halted ({st['reason']})" + (
                f"; {n} position{'s' if n != 1 else ''} sold at market, {c} resting entries cancelled" if flatten
                else "; open positions stay protected by their stops" + (f", {c} resting entr{'y' if c == 1 else 'ies'} cancelled" if c else "")
            )
            self.health.record("cycle", True, detail=f"HALT by {by}: {st['reason']}" + (" (flattened)" if flatten else ""), save=True)
            flat = "\n".join(f"sold {f['qty']} {f['symbol']} @ {f['price']:.2f}" + (f" ({f['r_multiple']:+.2f}R)" if f.get("r_multiple") is not None else "") for f in result["flattened"])
            self.alerts.halt(True, result["message"] + (f"\n{flat}" if flat else ""), by)
            log.warning("%s", result["message"])
        else:
            set_halt(self.state_dir, False)
            result["status"] = None
            result["message"] = "trading resumed: the next pass may open positions again"
            self.health.record("cycle", True, detail=f"RESUME by {by}", save=True)
            self.alerts.halt(False, result["message"], by)
            log.info("%s", result["message"])
        return result

    def last_report(self) -> dict | None:
        if self.report_path.exists():
            return json.loads(self.report_path.read_text())
        return None

    def state(self) -> TraderState:
        state = TraderState.load(self.state_path)
        state._persistence_path = self.state_path
        return state

    def open_positions(self) -> list[ManagedPosition]:
        return [ManagedPosition(**r) for r in self.state().managed.values()]
