"""Strategy 基类（口径对齐 Fx_Quant_System/BackTest/source/Strategy.h）与回测结果容器。"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from quant.cost import CostModel
from quant.data import DataFeed
from quant.engine import run as engine_run
from quant.trade import Trade


@dataclass
class BacktestResult:
    trades: list[Trade]
    stats: dict
    meta: dict = field(default_factory=dict)

    def to_json(self, path: str | Path) -> None:
        payload = {
            "meta": self.meta,
            "stats": self.stats,
            "trades": [t.__dict__ | {"direction": int(t.direction)} for t in self.trades],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")


class StrategyBase(ABC):
    """单品种单策略（D4：direction_mode ∈ {"long","short","both"}，v1 用 long/short）。"""

    type: str = "custom"

    def __init__(self, symbol: str, params: dict,
                 direction_mode: str = "long", total_capital: float = 100_000.0,
                 per_trade_lots: int = 1, rf_rate: float = 0.0,
                 cost_model: CostModel | None = None,
                 unit_value: float = 1.0):
        self.symbol = symbol
        self.params = dict(params)
        self.direction_mode = direction_mode
        self.total_capital = total_capital
        self.per_trade_lots = per_trade_lots
        self.rf_rate = rf_rate
        self.cost_model = cost_model or CostModel()
        self.unit_value = unit_value
        self.trades: list[Trade] = []

    @abstractmethod
    def prepare(self, feed: DataFeed, start_ts: int, end_ts: int) -> dict:
        """加载行情并预计算信号数组。返回 run 所需的 dict：
        data(OHLCV), band_upper, band_lower, tp_dist, sl_dist,
        be_enabled, be_trigger, be_buffer。"""

    def run_backtest(self, feed: DataFeed, start_ts: int, end_ts: int) -> BacktestResult:
        prep = self.prepare(feed, start_ts, end_ts)
        mode = {"long": 1, "short": 2, "both": 3}[self.direction_mode]
        trades = engine_run(
            prep["data"], prep["band_upper"], prep["band_lower"],
            prep["tp_dist"], prep["sl_dist"],
            instrument=self.symbol, direction_mode=mode,
            be_enabled=prep.get("be_enabled", False),
            be_trigger=prep.get("be_trigger", 0.0),
            be_buffer=prep.get("be_buffer", 0.0),
            cost_per_trade=self.cost_model.total_per_trade,
            unit_value=self.unit_value, quantity=self.per_trade_lots,
        )
        self.trades = trades
        stats = self.calc_cost_function(trades)
        meta = {"symbol": self.symbol, "strategy": self.type,
                "params": dict(self.params), "direction_mode": self.direction_mode,
                "start_ts": int(start_ts), "end_ts": int(end_ts),
                "cost_model": self.cost_model.__dict__ | {"total_per_trade":
                                                              self.cost_model.total_per_trade}}
        return BacktestResult(trades=trades, stats=stats, meta=meta)

    @staticmethod
    def calc_cost_function(trades: list[Trade]) -> dict:
        closed = [{"gross_pl": t.profit, "open_time": t.entry_time, "close_time": t.exit_time,
                   "commission": t.commission} for t in trades if t.closed]
        return compute_stats(closed)

    def output(self, result: BacktestResult, path: str | Path) -> None:
        result.to_json(path)


def compute_stats(closed: list[dict]) -> dict:
    """与实盘 /stats 同口径（fxcm_api.data.stats.compute_stats）；epoch 秒转 datetime。"""
    from datetime import datetime, timezone
    from fxcm_api.data.stats import compute_stats as _stats

    def to_dt(v):
        return datetime.fromtimestamp(int(v), tz=timezone.utc)

    rows = []
    for r in closed:
        rows.append({**r, "open_time": to_dt(r["open_time"]),
                     "close_time": to_dt(r["close_time"])})
    return _stats(closed_trades=rows)


def nan_forward_fill(a: np.ndarray) -> np.ndarray:
    """前向填充 NaN（保持头部 NaN：预热期不交易）。"""
    out = a.copy()
    mask = np.isnan(out)
    if mask.all() or not mask.any():
        return out
    idx = np.where(~mask, np.arange(len(out)), 0)
    np.maximum.accumulate(idx, out=idx)
    out[mask] = out[idx[mask]]
    return out
