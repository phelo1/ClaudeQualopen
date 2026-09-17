"""Offline intraday replay through the production strategy and paper executor.

Input timestamps label bar OPEN times and must include a timezone. Decisions
are made at bar close; market orders fill on the following available bar open.
No present-day context services or LLMs are queried during historical replay.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

from .broker import PaperBroker
from .config import StrategyConfig
from .persistence import atomic_json
from .trader import TraderState, LiveClock, run_cycle
from .market_calendar import NY


class ReplayBroker(PaperBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_bars = {}
        self.event_time = None

    def market_buy(self, symbol, qty, tag=""):
        order = self._new(symbol=symbol, side="buy", qty=qty, kind="market", tag=tag)
        self._save()
        return order

    def market_sell(self, symbol, qty, tag=""):
        order = self._new(symbol=symbol, side="sell", qty=qty, kind="market", tag=tag)
        self._save()
        return order

    def mark(self, bars, when=None):
        # The daily aggregate is visible to the detector, but fills use ONLY
        # the newly arriving intraday bar, never the earlier daily extrema.
        from .broker import Order
        fills = []
        for row in list(self.ledger.orders):
            if row["status"] == "open" and row["kind"] == "market" and row["symbol"] in self.event_bars:
                order = Order(**row)
                opening = float(self.event_bars[order.symbol]["open"])
                self._fill(order, opening * (1+self.slip if order.side == "buy" else 1-self.slip), self.event_time.isoformat())
                fills.append(order)
        return fills + super().mark(self.event_bars, self.event_time)


def load_intraday(directory: Path) -> dict:
    frames = {}
    for path in sorted(Path(directory).glob("*.csv")):
        frame = pd.read_csv(path)
        if "timestamp" not in frame:
            raise ValueError(f"{path.name}: timestamp column required (timezone-aware bar open)")
        stamps = [pd.Timestamp(v) for v in frame.pop("timestamp")]
        if not stamps or any(t.tzinfo is None for t in stamps):
            raise ValueError(f"{path.name}: explicit timezone required")
        frame.index = pd.DatetimeIndex(stamps).tz_convert("UTC")
        required = ["open", "high", "low", "close", "volume"]
        values = frame[required].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume < 0).any():
            raise ValueError(f"{path.name}: invalid OHLCV")
        if (frame.high < frame[["open", "close", "low"]].max(axis=1)).any() or (frame.low > frame[["open", "close"]].min(axis=1)).any():
            raise ValueError(f"{path.name}: inconsistent OHLC range")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError(f"{path.name}: timestamps must be unique and increasing")
        frames[path.stem.upper()] = frame[required]
    if not frames:
        raise ValueError("No timestamped intraday CSV files supplied")
    return frames


def replay(frames: dict, history: dict, cfg: StrategyConfig, output: Path, bar_minutes: int = 1, context_events: list | None = None) -> dict:
    from .autonomy import FrozenContext
    if not 1 <= bar_minutes <= 60:
        raise ValueError("bar_minutes must be 1..60")
    if cfg.reviewer.enabled or cfg.committee.enabled:
        raise ValueError("Historical replay requires reviewer.enabled=false and committee.enabled=false; current LLM verdicts are not historical evidence")
    if cfg.context.enabled and context_events is None:
        raise ValueError("Historical context is enabled; provide timestamped context events or explicitly disable it in the replay configuration")
    output = Path(output)
    if (output / "ledger.json").exists() or (output / "trader.json").exists():
        raise ValueError("Replay requires a fresh output directory; existing evidence is never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    broker = ReplayBroker(output / "ledger.json", cfg.risk.starting_equity, cfg.risk.slippage_bps, cfg.risk.commission_per_share)
    state = TraderState()
    state._persistence_path = output / "trader.json"
    times = sorted(set().union(*(set(frame.index) for frame in frames.values())))
    contexts = sorted(context_events or [], key=lambda e: pd.Timestamp(e["at"]))
    if any(pd.Timestamp(e["at"]).tzinfo is None for e in contexts):
        raise ValueError("Context availability timestamps must include a timezone")
    curve, missing = [], set()
    for timestamp in times:
        observed = timestamp + pd.Timedelta(minutes=bar_minutes)
        day = pd.Timestamp(timestamp.tz_convert(NY).date())
        available = {}
        broker.event_bars = {symbol: df.loc[timestamp] for symbol, df in frames.items() if timestamp in df.index}
        broker.event_time = timestamp
        for symbol, frame in frames.items():
            past = frame.loc[:timestamp]
            if past.empty:
                continue
            daily = past.groupby(past.index.tz_convert(NY).date).agg({"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"})
            daily.index = pd.to_datetime(daily.index)
            warmup = history.get(symbol, pd.DataFrame()).copy()
            if not warmup.empty:
                warmup = warmup.loc[warmup.index < daily.index[0]]
            available[symbol] = pd.concat([warmup, daily])
        frozen = {}
        for event in contexts:
            if pd.Timestamp(event["at"]) > observed:
                break
            frozen[event["symbol"]] = event["report"]
        gatherer = FrozenContext(frozen)
        pace = None
        if cfg.schedule.volume_pace:
            from .pace import project_volume
            available, pace = project_volume(available, observed.to_pydatetime(), min_fraction=cfg.schedule.pace_min_session_fraction)
        report = run_cycle(available, broker, cfg, state, asof=day, gatherer=gatherer if cfg.context.enabled else None, live=LiveClock(now=observed.to_pydatetime(), pace=pace), label="intraday_replay")
        missing.update(gatherer.missing)
        state.save(output / "trader.json")
        curve.append({"at": observed.isoformat(), "equity": broker.account().equity})
    result = {"execution_model": "intraday_next_bar_open", "bar_minutes": bar_minutes, "observations": len(curve), "equity": curve,
              "closed": state.closed, "open_positions": state.managed, "pending": state.pending,
              "missing_context_symbols": sorted(missing), "promotion_eligible": False,
              "assumptions": ["OHLC timestamps are bar opens; decisions follow their close", "Market orders fill at the next available bar open with configured costs",
                              "Within-bar stop/target conflicts use stop first", "No quote spread, queue priority, liquidity or partial-fill reconstruction", "Replay alone cannot authorize deployment"]}
    atomic_json(output / "replay.json", result)
    return result
