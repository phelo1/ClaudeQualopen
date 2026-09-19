"""Persistent autonomous research, prospective trials, promotion and rollback.

Discovery data never counts as prospective evidence. One challenger at a time
runs in an isolated paper account beside a frozen baseline, through run_cycle.
Only bounded selection knobs and a validated ranking model can be promoted.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .learning import KNOBS, KNOB_BY_KEY, config_value
from .persistence import atomic_json, desk_lock

FILE = "autonomy.json"
# Entry-time stop distance and profit target are frozen in ManagedPosition.
# Absolute risk limits and credentials are deliberately outside this search.
MANAGEMENT_BOUNDS = {"management.stop_adr_mult": (0.5, 2.0, 0.1), "management.partial_target_r": (1.0, 5.0, 0.5)}


def finite_metrics(metrics):
    return {k: (None if isinstance(v, (float, np.floating)) and not np.isfinite(v) else v) for k, v in metrics.items()}


def fingerprint(cfg: StrategyConfig) -> str:
    raw = cfg.to_dict()
    raw.pop("autonomy", None)
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:20]


def read(directory: Path) -> dict:
    path = Path(directory) / FILE
    if not path.exists():
        return {"version": 1, "active": {"id": "baseline", "overrides": {}, "model": None}, "trial": None, "history": [], "research": {"status": "waiting"}}
    return json.loads(path.read_text(encoding="utf-8"))


def valid_overrides(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        knob = KNOB_BY_KEY.get(key)
        if knob and isinstance(value, (int, float)) and np.isfinite(value) and knob.lo <= value <= knob.hi:
            out[key] = value
        elif key in MANAGEMENT_BOUNDS and isinstance(value, (int, float)) and np.isfinite(value):
            low, high, _ = MANAGEMENT_BOUNDS[key]
            if low <= value <= high:
                out[key] = value
    return out


def active_policy(directory: Path, cfg: StrategyConfig) -> dict:
    state = read(directory)
    active = state.get("active", {})
    if not cfg.autonomy.enabled or (active.get("base_hash") and active["base_hash"] != fingerprint(cfg)):
        return {"id": "baseline", "overrides": {}, "model": None}
    return {**active, "overrides": valid_overrides(active.get("overrides", {}))}


def score(metrics: dict) -> float:
    if metrics.get("trades", 0) < 10:
        return float("-inf")
    return float(metrics.get("total_return", 0)) - 2 * abs(float(metrics.get("max_drawdown", 0)))


def candidates(cfg: StrategyConfig, maximum: int) -> list[dict]:
    # Intraday confirmation and options thresholds need their own recorded inputs;
    # a daily OHLC backtest cannot select them credibly.
    keys = {"breakout.min_breakout_volume_ratio", "breakout.max_flag_depth", "breakout.max_gap_pct", "momentum.min_adr_pct", "themes.min_theme_percentile"}
    result = []
    for knob in KNOBS:
        if knob.key not in keys:
            continue
        current = config_value(cfg, knob.key)
        for value in (knob.tighter(current), knob.looser(current)):
            if value != current:
                result.append({knob.key: value})
    for key, (low, high, step) in MANAGEMENT_BOUNDS.items():
        current = config_value(cfg, key)
        if current is not None:
            for value in (max(low, current-step), min(high, current+step)):
                if value != current:
                    result.append({key: round(value, 4)})
    # Rotate a bounded candidate budget across the entire search space rather
    # than starving later stop/target candidates every week.
    if len(result) > maximum:
        offset = (pd.Timestamp.now(tz="UTC").isocalendar().week * maximum) % len(result)
        result = (result + result)[offset:offset+maximum]
    return result[:maximum]


def research(directory: Path, frames: dict, cfg: StrategyConfig, closed: list, shadows: list) -> dict:
    """CPU-heavy discovery uses its own lease, leaving the execution desk free."""
    from .backtest import run_backtest
    from .outcome_model import train
    directory = Path(directory)
    with desk_lock(directory / "autonomy", timeout=1):
        state = read(directory)
        if not cfg.autonomy.enabled:
            return {"status": "disabled"}
        if state.get("trial") and state["trial"]["status"] in ("forward", "canary", "monitoring"):
            return {"status": "trial_in_progress", "id": state["trial"]["id"]}
        dates = pd.DatetimeIndex(sorted(set().union(*(set(df.index) for df in frames.values()))))
        today = pd.Timestamp.now(tz="America/New_York").date()
        dates = dates[[d.date() < today for d in dates]]  # only completed sessions
        required = max(cfg.autonomy.min_history_days, cfg.warmup_bars + 100)
        if len(dates) < required:
            state["research"] = {"status": "collecting", "reason": f"Need {required} completed bars; have {len(dates)}"}
            atomic_json(directory / FILE, state)
            return state["research"]
        active = active_policy(directory, cfg)
        # Refresh outcome evidence even while historical parameter research is
        # waiting for fresh sessions. Deployment still requires a forward trial.
        model = train(closed, shadows, cfg.autonomy.min_model_records) if cfg.autonomy.train_outcome_model else {"status": "disabled"}
        state["model_training"] = model
        baseline = cfg.with_overrides(active.get("overrides", {}))
        holdout = dates[-60:]
        if state.get("last_holdout_end"):
            holdout = dates[dates > pd.Timestamp(state["last_holdout_end"])]
        if len(holdout) < 20:
            state["research"] = {"status": "collecting", "reason": "Holdout already consumed; waiting for 20 fresh sessions"}
            atomic_json(directory / FILE, state)
            return state["research"]
        train_end = dates[dates < holdout[0]][-1]
        training_frames = {s: df.loc[:train_end] for s, df in frames.items()}
        selection_start = str(dates[cfg.warmup_bars].date())
        options = []
        for params in candidates(baseline, cfg.autonomy.max_candidates):
            metrics = finite_metrics(run_backtest(training_frames, baseline.with_overrides(params), start=selection_start, end=str(train_end.date())).metrics())
            options.append({"overrides": params, "score": score(metrics), "metrics": metrics})
        qualifying = [o for o in options if np.isfinite(o["score"])]
        # Reserve/consume the holdout before inspecting it; a crash cannot cause
        # the same holdout to become a new independent experiment.
        state["last_holdout_end"] = str(holdout[-1].date())
        state["research"] = {"status": "evaluating", "asof": str(dates[-1].date()),
                             "trials": [{**o, "score": o["score"] if np.isfinite(o["score"]) else None} for o in options]}
        atomic_json(directory / FILE, state)
        chosen, validation = None, None
        if qualifying:
            best = max(qualifying, key=lambda o: o["score"])
            start, end = str(holdout[0].date()), str(holdout[-1].date())
            base = finite_metrics(run_backtest(frames, baseline, start=start, end=end).metrics())
            trial = finite_metrics(run_backtest(frames, baseline.with_overrides(best["overrides"]), start=start, end=end).metrics())
            validation = {"baseline": base, "candidate": trial, "from": start, "through": end}
            if trial.get("trades", 0) >= 10 and trial.get("total_return", 0) > base.get("total_return", 0) + cfg.autonomy.minimum_return_lift and abs(trial.get("max_drawdown", 1)) <= cfg.autonomy.max_drawdown:
                chosen = {"kind": "parameters", "overrides": {**active.get("overrides", {}), **best["overrides"]}, "model": active.get("model")}
        if chosen is None and model.get("status") == "candidate" and model.get("id") != (active.get("model") or {}).get("id"):
            chosen = {"kind": "outcome_model", "overrides": active.get("overrides", {}), "model": model}
        state["research"].update(status="candidate" if chosen else "no_candidate", validation=validation)
        if chosen:
            trial_id = uuid.uuid4().hex[:12]
            # Record input identities; later provider revisions are detectable.
            manifest = {s: {"rows": len(df), "sha256": hashlib.sha256(df.to_csv().encode()).hexdigest()} for s, df in frames.items()}
            atomic_json(directory / "autonomy" / trial_id / "data_manifest.json", manifest)
            state["trial"] = {"id": trial_id, "status": "forward", "created_at": datetime.now(timezone.utc).isoformat(),
                              "discovery_end": str(dates[-1].date()), "base_hash": fingerprint(cfg),
                              "baseline": active, "candidate": {"id": trial_id, "base_hash": fingerprint(cfg), **chosen},
                              "config": cfg.to_dict(), "observations": [], "attempts": len(options), "coverage_gaps": []}
            state["history"].append({"event": "registered", "id": trial_id, "at": state["trial"]["created_at"], "kind": chosen["kind"]})
        atomic_json(directory / FILE, state)
        return state["research"]


def paired_interval(observations: list[dict], attempts: int = 1) -> dict:
    """Moving-block bootstrap of paired daily returns (five-session blocks)."""
    # Last observation of each day; intraday polls are not independent samples.
    days = {o["date"]: o for o in observations}
    rows = [days[d] for d in sorted(days)]
    if len(rows) < 6:
        return {"days": len(rows), "lower": None, "upper": None, "lift": 0.0}
    baseline = np.asarray([1.0, *[r["baseline"] for r in rows]])
    candidate = np.asarray([1.0, *[r["candidate"] for r in rows]])
    diff = np.diff(candidate)/candidate[:-1] - np.diff(baseline)/baseline[:-1]
    rng = np.random.default_rng(917)
    n, block = len(diff), min(5, len(diff))
    bootstrap = []
    for _ in range(2000):
        starts = rng.integers(0, n, size=(n+block-1)//block)
        draw = np.concatenate([diff[(s+np.arange(block)) % n] for s in starts])[:n]
        bootstrap.append(float(draw.mean()))
    alpha = max(0.001, 0.025 / max(attempts, 1))
    return {"days": n, "lower": float(np.quantile(bootstrap, alpha)), "upper": float(np.quantile(bootstrap, 1-alpha)),
            "lift": float(candidate[-1] - baseline[-1]),
            "drawdown": float(np.min(candidate/np.maximum.accumulate(candidate)-1))}


class FrozenContext:
    def __init__(self, rows):
        from .context.base import ContextReport
        self.rows = {s: ContextReport.from_dict(r) for s, r in rows.items()}
        self.missing = set()

    def gather(self, symbols):
        self.missing.update(set(symbols)-set(self.rows))
        return {s: self.rows[s] for s in symbols if s in self.rows}


def observe(directory: Path, frames: dict, cfg: StrategyConfig, report, live, live_broker: bool, journal: list) -> dict:
    """Prospective baseline/challenger accounts share the same observation clock."""
    from .broker import PaperBroker
    from .trader import TraderState, run_cycle
    directory = Path(directory)
    with desk_lock(directory / "autonomy", timeout=1):
        state = read(directory)
        trial = state.get("trial")
        if not cfg.autonomy.enabled or not trial or trial["status"] not in ("forward", "canary", "monitoring"):
            return state
        if trial["base_hash"] != fingerprint(cfg):
            trial["status"] = "invalidated"
            trial["reason"] = "Operator configuration changed; old trial no longer describes this strategy"
            state["active"] = {"id": "baseline", "overrides": {}, "model": None}
            atomic_json(directory / FILE, state)
            return state
        observed = live.now.isoformat() if live.now else report.asof
        if observed <= trial.get("last_observed", "") or report.asof <= trial["discovery_end"]:
            return state
        if report.halted:
            trial["coverage_gaps"] = sorted(set(trial.get("coverage_gaps", []) + ["Desk halted during prospective observation"]))
            atomic_json(directory / FILE, state)
            return state
        values, counts = {}, {}
        coverage = list(trial.get("coverage_gaps", []))
        for name in ("baseline", "candidate"):
            policy = trial[name]
            policy_cfg = StrategyConfig.from_dict(trial["config"]).with_overrides(valid_overrides(policy.get("overrides", {})))
            folder = directory / "autonomy" / trial["id"] / name
            broker = PaperBroker(folder / "ledger.json", starting_cash=policy_cfg.risk.starting_equity, slippage_bps=policy_cfg.risk.slippage_bps, commission_per_share=policy_cfg.risk.commission_per_share)
            book = TraderState.load(folder / "trader.json")
            book._persistence_path = folder / "trader.json"
            book.policy = policy
            checkpoint = folder / "observation.json"
            prior = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
            if prior.get("at") != observed:
                if prior.get("running"):
                    coverage.append(f"{name}: interrupted observation; trial cannot authorize promotion")
                atomic_json(checkpoint, {"at": observed, "running": True})
                context = FrozenContext(report.context)
                shadow_report = run_cycle(frames, broker, policy_cfg, book, asof=pd.Timestamp(report.asof), gatherer=context,
                                          full_scan=report.scan == "full", scan_symbols=set(report.scope), live=live,
                                          regime=(report.regime_ok, report.regime_note, report.regime_known), label="prospective_trial")
                book.save(folder / "trader.json")
                if context.missing and policy_cfg.context.enabled:
                    coverage.append(f"{name}: candidate context records unavailable")
                if not report.regime_known:
                    coverage.append(f"{name}: regime unavailable")
                atomic_json(checkpoint, {"at": observed, "running": False})
            elif prior.get("running"):
                coverage.append(f"{name}: interrupted observation; trial cannot authorize promotion")
            values[name] = broker.account().equity / policy_cfg.risk.starting_equity
            counts[name] = len(book.closed)
        trial["observations"].append({"at": observed, "date": report.asof, **values})
        trial["last_observed"] = observed
        trial["coverage_gaps"] = sorted(set(coverage))
        trial["trades"] = counts
        evaluation = paired_interval(trial["observations"], trial["attempts"])
        trial["evaluation"] = evaluation
        controls = cfg.autonomy
        created = pd.Timestamp(trial["created_at"])
        clock = pd.Timestamp(live.now) if live.now else pd.Timestamp.now(tz="UTC")
        age = (clock.tz_convert("UTC") - created).days if clock.tzinfo else 0
        eligible = (not coverage and evaluation["days"] >= controls.min_forward_days and counts["candidate"] >= controls.min_forward_trades
                    and evaluation["lower"] is not None and evaluation["lower"] > 0
                    and evaluation["lift"] >= controls.minimum_return_lift and abs(evaluation["drawdown"]) <= controls.max_drawdown)
        if trial["status"] == "forward" and eligible and controls.auto_promote:
            state["active"] = {**trial["candidate"], "previous": trial["baseline"], "stage": "canary" if live_broker else "active", "promoted_at": observed}
            trial["status"] = "canary" if live_broker else "monitoring"
            trial["promoted_at"] = observed
            state["history"].append({"event": "promoted", "id": trial["id"], "at": observed, "evaluation": evaluation})
        elif trial["status"] == "forward" and age > controls.max_trial_days:
            trial["status"] = "expired"
            state["history"].append({"event": "expired", "id": trial["id"], "at": observed})
        elif trial["status"] in ("canary", "monitoring"):
            fresh = [r for r in journal if (r.get("features") or {}).get("policy_version") == trial["id"]
                     and r.get("evidence") == "broker_verified" and r.get("fees_known")]
            actual_r = sum(float(r.get("r_multiple", 0)) for r in fresh)
            post = [o for o in trial["observations"] if o["at"] >= trial["promoted_at"]]
            if post:
                origins = {name: post[0][name] for name in ("baseline", "candidate")}
                post = [{**o, **{name:o[name]/origins[name] for name in origins}} for o in post]
            after = paired_interval(post, trial["attempts"])
            trial["post_promotion"] = after
            rollback = abs(after.get("drawdown", 0)) > controls.rollback_drawdown or (after.get("upper") is not None and after["upper"] < 0)
            rollback = rollback or (len(fresh) >= 10 and actual_r <= -5)
            if rollback:
                state["active"] = trial["baseline"]
                trial["status"] = "rolled_back"
                state["history"].append({"event": "rolled_back", "id": trial["id"], "at": observed, "actual_r": actual_r, "evaluation": evaluation})
            elif trial["status"] == "canary" and len(fresh) >= controls.canary_trades and actual_r > 0:
                state["active"]["stage"] = "active"
                trial["status"] = "monitoring"
                state["history"].append({"event": "canary_completed", "id": trial["id"], "at": observed})
            elif trial["status"] == "monitoring" and (pd.Timestamp(observed).date()-pd.Timestamp(trial["promoted_at"]).date()).days >= 60:
                trial["status"] = "accepted"
                atomic_json(directory / "autonomy" / trial["id"] / "completed_trial.json", trial)
                state["history"].append({"event": "accepted", "id": trial["id"], "at": observed})
        atomic_json(directory / FILE, state)
        return state


def monitor_deployment(directory: Path, cfg: StrategyConfig, equity: float) -> dict:
    """Continue rollback protection even between prospective experiments."""
    with desk_lock(Path(directory) / "autonomy", timeout=1):
        state = read(directory)
        active = state.get("active", {})
        if not cfg.autonomy.enabled or not active.get("previous") or not np.isfinite(equity) or equity <= 0:
            return state
        peak = max(float(active.get("equity_peak", equity)), equity)
        active["equity_peak"] = peak
        active["equity_drawdown"] = equity/peak-1
        if abs(active["equity_drawdown"]) > cfg.autonomy.rollback_drawdown:
            state["history"].append({"event": "rolled_back", "id": active["id"], "at": datetime.now(timezone.utc).isoformat(), "reason": "Account drawdown exceeded deployment bound"})
            state["active"] = active["previous"]
            if state.get("trial") and state["trial"]["status"] in ("forward", "canary", "monitoring"):
                state["trial"]["status"] = "invalidated"
                state["trial"]["reason"] = "Deployment rollback changed baseline"
        atomic_json(Path(directory) / FILE, state)
        return state
