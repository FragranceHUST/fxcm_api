"""实时行情接入：OFFERS 更新 → 内存多周期聚合 + 收盘K线落库（SQLite）。

绑定 real 会话（数据面唯一来源）。落库周期 = 1m/15m/1h/4h；1s 仅内存（实时刷新用）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from forexconnect import ForexConnect, TableListener

from fxcm_api.candles import GRANULARITIES, CandleAggregator
from fxcm_api.data.store import CandleStore
from fxcm_api.trading import find_offer

logger = logging.getLogger("fxcm_api.data.hub")

# 收盘后落库的周期（秒）；1s 不落库
PERSIST_TFS: tuple[int, ...] = (60, 900, 3600, 14400)


class MarketHub:
    def __init__(self, fx, symbols: list[str], store: CandleStore):
        self.symbols = list(symbols)
        self.store = store
        self.aggregators: dict[str, CandleAggregator] = {}
        self._offer_ids: dict[str, str] = {}
        self._ws_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

        for symbol in self.symbols:
            self.aggregators[symbol] = CandleAggregator(symbol)

        self._listener = TableListener(on_changed_callback=self._on_changed)
        self._bind(fx)
        logger.info("OFFERS 订阅已建立：%s", ", ".join(self.symbols))

    def _bind(self, fx) -> None:
        self._offer_ids.clear()
        for symbol in self.symbols:
            offer = find_offer(fx, symbol)
            self._offer_ids[offer.offer_id] = symbol
        self._listener.subscribe(fx.get_table(ForexConnect.OFFERS))

    def resubscribe(self, fx) -> None:
        """会话重连后换绑 OFFERS 订阅：旧订阅随旧会话死亡，不换绑则行情永久停更。"""
        self._bind(fx)
        logger.warning("OFFERS 订阅已换绑到新会话")

    def subscribe_ws(self) -> asyncio.Event:
        """注册 WS 推送订阅（在事件循环线程调用）；返回该连接专属唤醒事件。"""
        loop = asyncio.get_running_loop()
        evt = asyncio.Event()
        self._ws_subscribers.add((loop, evt))
        return evt

    def unsubscribe_ws(self, evt: asyncio.Event) -> None:
        self._ws_subscribers = {(l, e) for l, e in self._ws_subscribers if e is not evt}

    def _notify_ws(self) -> None:
        for loop, evt in list(self._ws_subscribers):
            if loop is None:
                self._ws_subscribers.discard((loop, evt))
                continue
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                self._ws_subscribers.discard((loop, evt))   # 连接所属循环已关闭

    def _on_changed(self, _listener, _row_id, row) -> None:
        symbol = self._offer_ids.get(row.offer_id)
        if symbol is None:
            return
        try:
            bid, ask = float(row.bid), float(row.ask)
        except (TypeError, ValueError):
            return
        if bid <= 0 or ask <= 0:
            return
        agg = self.aggregators[symbol]
        before = {g: self._last_bar_ts(agg, g) for g in PERSIST_TFS}
        agg.on_tick(time.time(), bid, ask)
        self._notify_ws()
        for g in PERSIST_TFS:
            b = before[g]
            if b is None:
                continue
            for bar in agg.candles(g, limit=2):
                if bar.ts == b:      # 该桶已收盘（出现了更新的一根）
                    self.store.upsert_bid_candles(
                        symbol, g, [(bar.ts, bar.open, bar.high, bar.low,
                                     bar.close, bar.volume)])
                    break

    @staticmethod
    def _last_bar_ts(agg: CandleAggregator, g: int) -> int | None:
        bars = agg.candles(g, limit=1)
        return bars[-1].ts if bars else None

    def candles(self, symbol: str, tf: int, limit: int = 500) -> list:
        """标准化视图：历史（库）+ 正在形成的K线（内存）合并，内存优先。"""
        bars = self.store.get_candles(symbol, tf, limit=limit)
        if tf not in GRANULARITIES:          # 如 1d：纯库数据（回填提供）
            return bars
        forming = self.aggregators[symbol].candles(tf, limit=1)
        if forming:
            if bars and forming[0].ts == bars[-1].ts:
                bars[-1] = forming[0]
            elif not bars or forming[0].ts > bars[-1].ts:
                bars.append(forming[0])
        return bars

    def last_tick(self, symbol: str) -> tuple[float, float, float] | None:
        return self.aggregators[symbol].last_tick()

    def close(self) -> None:
        self._listener.unsubscribe()
