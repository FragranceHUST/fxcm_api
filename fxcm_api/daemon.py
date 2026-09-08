"""常驻 daemon：单次登录 + OFFERS 订阅（K线聚合 / CSV 落盘）+ guard 循环 + Web 服务。

用法：
  .venv/bin/python -m fxcm_api.daemon --config config.demo.json
  浏览器访问 http://127.0.0.1:8911/  （只读盯盘页，无下单功能）

架构：
  fxcorepy 回调线程 → MarketHub（聚合 + CSV） ← FastAPI 线程（REST/WS 只读快照）
  guard 独立线程（StopManager.run_forever），与 Web 共享同一会话，避免重复登录。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from forexconnect import ForexConnect, TableListener

from fxcm_api.candles import TF_LABELS, CandleAggregator
from fxcm_api.config import (
    Credentials,
    DaemonSettings,
    GuardSettings,
    load_config,
    load_daemon_settings,
)
from fxcm_api.csv_store import CsvTickStore
from fxcm_api.session import connect, disconnect
from fxcm_api.stop_manager import StopManager
from fxcm_api.trading import find_offer, trade_is_buy

logger = logging.getLogger("fxcm_api.daemon")


class MarketHub:
    """OFFERS 更新事件 → 每品种 CandleAggregator + 已收盘 1s 桶落盘 CSV。"""

    def __init__(self, fx, symbols: list[str], data_dir: str):
        self.symbols = list(symbols)
        self.aggregators: dict[str, CandleAggregator] = {}
        self.stores: dict[str, CsvTickStore] = {}
        self._offer_ids: dict[str, str] = {}

        offers_table = fx.get_table(ForexConnect.OFFERS)
        for symbol in self.symbols:
            offer = find_offer(fx, symbol)
            self._offer_ids[offer.offer_id] = symbol
            agg = CandleAggregator(symbol)
            store = CsvTickStore(data_dir, symbol)
            history = store.load()
            agg.load_history(history)
            logger.info("%s: 从 CSV 恢复 %d 根 1s K线", symbol, len(history))
            self.aggregators[symbol] = agg
            self.stores[symbol] = store

        self._listener = TableListener(on_changed_callback=self._on_changed)
        self._listener.subscribe(offers_table)
        logger.info("OFFERS 订阅已建立：%s", ", ".join(self.symbols))

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
        before = agg.candles(1, limit=1)
        agg.on_tick(time.time(), bid, ask)
        after = agg.candles(1, limit=1)
        # 1s 桶滚动 → 上一根已收盘，追加落盘
        if before and after and before[0].ts != after[0].ts:
            self.stores[symbol].append(before[0])

    def close(self) -> None:
        self._listener.unsubscribe()
        for store in self.stores.values():
            store.close()


def build_app(hub: MarketHub, fx, daemon_cfg: DaemonSettings) -> FastAPI:
    app = FastAPI(title="FXCM Watch Daemon", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health():
        status = fx.session.session_status.name if fx.session else "NONE"
        return {"status": status, "symbols": hub.symbols,
                "account": daemon_cfg.watch_symbols, "ts": time.time()}

    @app.get("/api/quotes")
    def quotes():
        out = {}
        for symbol, agg in hub.aggregators.items():
            tick = agg.last_tick()
            if tick:
                bid, ask, ts = tick
                out[symbol] = {"bid": bid, "ask": ask, "ts": ts}
        return out

    @app.get("/api/candles")
    def candles(symbol: str, tf: str = "1s", limit: int = 500):
        agg = hub.aggregators.get(symbol)
        if agg is None:
            raise HTTPException(404, f"未知品种: {symbol}")
        granularity = TF_LABELS.get(tf)
        if granularity is None:
            raise HTTPException(400, f"tf 须为 {'/'.join(TF_LABELS)}")
        bars = agg.candles(granularity, limit=max(1, min(limit, 2000)))
        return [b.to_dict() for b in bars]

    @app.get("/api/positions")
    def positions():
        offers = {row.offer_id: row for row in fx.get_table(ForexConnect.OFFERS)}
        trades = fx.get_table(ForexConnect.TRADES)
        out = []
        for row in trades or []:
            offer = offers.get(row.offer_id)
            out.append({
                "trade_id": row.trade_id,
                "symbol": offer.instrument if offer else "?",
                "is_buy": trade_is_buy(row),
                "amount": row.amount,
                "open_rate": row.open_rate,
                "stop": row.stop,
                "stop_order_id": row.stop_order_id or "",
                "gross_pl": row.gross_pl,
            })
        return out

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, symbol: str | None = None):
        await websocket.accept()
        targets = [symbol] if symbol in hub.aggregators else list(hub.aggregators)
        try:
            while True:
                payload: dict = {"ts": time.time(), "quotes": {}}
                for target in targets:
                    agg = hub.aggregators[target]
                    tick = agg.last_tick()
                    if not tick:
                        continue
                    bid, ask, ts = tick
                    entry: dict = {"bid": bid, "ask": ask, "ts": ts}
                    last = agg.candles(1, limit=1)
                    if last:
                        entry["last_1s"] = last[0].to_dict()
                    payload["quotes"][target] = entry
                await websocket.send_json(payload)
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass

    app.mount("/static", StaticFiles(directory="static"), name="static")

    @app.get("/")
    def index():
        return FileResponse("static/index.html")

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FXCM 常驻盯盘 daemon")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cred: Credentials
    guard: GuardSettings
    cred, guard = load_config(args.config)
    daemon_cfg = load_daemon_settings(args.config)
    port = args.port or daemon_cfg.port

    fx = None
    hub = None
    try:
        fx = connect(cred, retries=4)
        hub = MarketHub(fx, daemon_cfg.watch_symbols, daemon_cfg.data_dir)
        for store in hub.stores.values():
            store.open_for_append()

        if daemon_cfg.guard_enabled:
            manager = StopManager(fx, guard)
            threading.Thread(target=manager.run_forever, name="guard", daemon=True).start()
            logger.info("guard 循环已启动 (dry_run=%s)", guard.dry_run)

        app = build_app(hub, fx, daemon_cfg)
        logger.info("Web 服务: http://%s:%d/ (只读)", args.host, port)
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
        return 0
    finally:
        if hub is not None:
            hub.close()
        disconnect(fx)


if __name__ == "__main__":
    raise SystemExit(main())
