"""K线聚合：tick → 1s/1m/15m/1h 多周期，线程安全，供 daemon 与 Web 端共用。

设计要点：
- 单把 threading.Lock 保护全部状态（fxcorepy 回调线程写，FastAPI 线程读）；
- 每 tick 同时推进所有周期（桶起点 = floor(ts / g) * g），高周期天然由低频 tick 更新；
- 断线重启恢复：用已存的 1s 序列 rebuild 出高周期（见 rebuild）。
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import asdict, dataclass

# 默认周期（秒）：1s / 1m / 15m / 1h
GRANULARITIES: tuple[int, ...] = (1, 60, 900, 3600)
# 粒度标签 ↔ 秒数（API 参数映射用）
TF_LABELS: dict[str, int] = {"1s": 1, "1m": 60, "15m": 900, "1h": 3600}
# 每周期内存保留的K线数量上限（1s×2000 ≈ 33 分钟历史，重启后靠 CSV 加载补齐）
MAX_CANDLES = 2000


@dataclass
class Candle:
    ts: int        # 桶起点 epoch 秒
    open: float
    high: float
    low: float
    close: float
    volume: int    # tick 计数

    def to_dict(self) -> dict:
        return asdict(self)


def bucket_start(ts: float, granularity: int) -> int:
    """ts（epoch 秒）所在桶的起点。"""
    return int(math.floor(ts / granularity) * granularity)


def _apply_tick(series: deque[Candle], ts: float, price: float, granularity: int) -> None:
    """向单个周期序列喂一个 tick（调用方需已持锁）。"""
    bts = bucket_start(ts, granularity)
    if series and series[-1].ts == bts:
        c = series[-1]
        c.high = max(c.high, price)
        c.low = min(c.low, price)
        c.close = price
        c.volume += 1
        return
    series.append(Candle(ts=bts, open=price, high=price, low=price, close=price, volume=1))
    while len(series) > MAX_CANDLES:
        series.popleft()


def rebuild(candles_1s: list[Candle], granularity: int) -> list[Candle]:
    """由 1s 序列聚合出高周期序列（用于启动时从 CSV 恢复）。"""
    out: list[Candle] = []
    for c in candles_1s:
        bts = bucket_start(c.ts, granularity)
        if out and out[-1].ts == bts:
            last = out[-1]
            last.high = max(last.high, c.high)
            last.low = min(last.low, c.low)
            last.close = c.close
            last.volume += c.volume
        else:
            out.append(Candle(ts=bts, open=c.open, high=c.high, low=c.low,
                              close=c.close, volume=c.volume))
    return out


class CandleAggregator:
    """单品种多周期聚合器。price 取 bid（与止损逻辑一致，图表口径统一）。

    on_tick 由 fxcorepy 回调线程调用；candles/last_tick 由 API 线程调用。
    """

    def __init__(self, symbol: str, granularities: tuple[int, ...] = GRANULARITIES):
        self.symbol = symbol
        self._granularities = tuple(granularities)
        self._lock = threading.Lock()
        self._series: dict[int, deque[Candle]] = {g: deque() for g in self._granularities}
        self._last_bid: float | None = None
        self._last_ask: float | None = None
        self._last_ts: float | None = None
        self._tick_count = 0

    def on_tick(self, ts: float, bid: float, ask: float) -> None:
        with self._lock:
            self._last_bid = bid
            self._last_ask = ask
            self._last_ts = ts
            self._tick_count += 1
            for g in self._granularities:
                _apply_tick(self._series[g], ts, bid, g)

    def load_history(self, candles_1s: list[Candle]) -> None:
        """启动时从 CSV 恢复：填充 1s 序列、rebuild 高周期，并用尾根K线播种 last_tick。"""
        if not candles_1s:
            return
        with self._lock:
            for c in candles_1s:
                self._series[1].append(c)
            while len(self._series[1]) > MAX_CANDLES:
                self._series[1].popleft()
            for g in self._granularities:
                if g > 1:
                    rebuilt = rebuild(list(self._series[1]), g)
                    self._series[g] = deque(rebuilt[-MAX_CANDLES:])
            last = candles_1s[-1]
            self._last_bid = self._last_ask = last.close
            self._last_ts = float(last.ts)

    def candles(self, granularity: int, limit: int = 500) -> list[Candle]:
        """快照拷贝（按时间升序，最多 limit 根）。"""
        with self._lock:
            series = self._series.get(granularity)
            if not series:
                return []
            return [c for c in list(series)[-limit:]]

    def last_tick(self) -> tuple[float, float, float] | None:
        """(bid, ask, ts) 快照；尚无 tick 时返回 None。"""
        with self._lock:
            if self._last_bid is None or self._last_ask is None or self._last_ts is None:
                return None
            return self._last_bid, self._last_ask, self._last_ts

    def stats(self) -> dict:
        with self._lock:
            return {
                "symbol": self.symbol,
                "tick_count": self._tick_count,
                "candles": {str(g): len(s) for g, s in self._series.items()},
                "last_bid": self._last_bid,
                "last_ask": self._last_ask,
            }
