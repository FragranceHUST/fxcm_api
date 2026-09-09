"""常驻 daemon：双会话（real 数据面 + demo 账户面）+ guard 循环 + Web 服务。

用法：
  .venv/bin/python -m fxcm_api.daemon --config config.demo.json [--config-real config.json]
  浏览器访问 http://127.0.0.1:8911/  （Web 可切换 demo/real 环境）

架构：
  real 会话：OFFERS 订阅 → MarketHub（聚合 + SQLite 落库）→ 图表数据（两环境共用）
  demo 会话：guard 循环 + demo 账户/交易
  real 会话：guard 循环（若其配置开启）+ real 账户/交易
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from forexconnect import ForexConnect

from fxcm_api.candles import TF_LABELS
from fxcm_api.config import DaemonSettings, load_daemon_settings
from fxcm_api.data.hub import MarketHub
from fxcm_api.data.store import CandleStore
from fxcm_api.sessions import SessionManager, SessionWorker
from fxcm_api.stop_manager import StopManager
from fxcm_api.trading import trade_is_buy

logger = logging.getLogger("fxcm_api.daemon")


def _positions_snapshot(fx) -> list[dict]:
    if fx is None:
        return []
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


def build_app(hub: MarketHub, mgr: SessionManager, store: CandleStore,
              daemon_cfg: DaemonSettings) -> FastAPI:
    app = FastAPI(title="FXCM Watch Daemon", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health():
        return {"status": "OK", "symbols": hub.symbols,
                "environments": mgr.status(), "ts": time.time()}

    @app.get("/api/environments")
    def environments():
        out = {}
        for env in ("real", "demo"):
            worker = mgr.worker(env)
            entry = worker.status()
            entry["account"] = worker.account_summary()
            out[env] = entry
        return out

    @app.get("/api/quotes")
    def quotes():
        out = {}
        for symbol in hub.symbols:
            tick = hub.last_tick(symbol)
            if tick:
                bid, ask, ts = tick
                out[symbol] = {"bid": bid, "ask": ask, "ts": ts}
        return out

    @app.get("/api/candles")
    def candles(symbol: str, tf: str = "1m", limit: int = 500):
        if symbol not in hub.aggregators:
            raise HTTPException(404, f"未知品种: {symbol}")
        granularity = TF_LABELS.get(tf)
        if granularity is None:
            raise HTTPException(400, f"tf 须为 {'/'.join(TF_LABELS)}")
        bars = hub.candles(symbol, granularity, limit=max(1, min(limit, 5000)))
        return [b.to_dict() for b in bars]

    @app.get("/api/{env}/positions")
    def positions(env: str):
        if env not in ("real", "demo"):
            raise HTTPException(404, f"未知环境: {env}")
        return _positions_snapshot(mgr.worker(env).fx)

    @app.get("/api/positions")
    def positions_legacy():
        return _positions_snapshot(mgr.worker("demo").fx)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, symbol: str | None = None):
        await websocket.accept()
        targets = [symbol] if symbol in hub.aggregators else list(hub.aggregators)
        try:
            while True:
                payload: dict = {"ts": time.time(), "quotes": {}}
                for target in targets:
                    tick = hub.last_tick(target)
                    if not tick:
                        continue
                    bid, ask, ts = tick
                    payload["quotes"][target] = {"bid": bid, "ask": ask, "ts": ts}
                await websocket.send_json(payload)
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass

    app.mount("/static", StaticFiles(directory="static"), name="static")

    @app.get("/")
    def index():
        return FileResponse("static/index.html")

    return app


def _start_guard(worker: SessionWorker) -> None:
    if not worker.daemon_cfg.guard_enabled:
        return
    manager = StopManager(worker.fx, worker.guard)
    threading.Thread(target=manager.run_forever, name=f"guard-{worker.env}",
                     daemon=True).start()
    logger.info("[%s] guard 循环已启动 (dry_run=%s, 品种=%s)",
                worker.env, worker.guard.dry_run, worker.guard.symbol_filter or "全品种")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FXCM 常驻盯盘 daemon（双环境）")
    parser.add_argument("--config", default="config.demo.json",
                        help="demo 环境配置 + daemon 设置")
    parser.add_argument("--config-real", default="config.json", help="real 环境配置")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    daemon_cfg = load_daemon_settings(args.config)
    port = args.port or daemon_cfg.port

    mgr = SessionManager(real_config=args.config_real, demo_config=args.config)
    ok_real, ok_demo = mgr.start()
    if not ok_real:
        logger.error("real 会话未能建立（数据面依赖），退出")
        mgr.stop()
        return 1
    if not ok_demo:
        logger.warning("demo 会话未就绪，仅 real 数据面可用")

    hub = None
    store = None
    try:
        store = CandleStore(Path(daemon_cfg.data_dir) / "candles.db")
        hub = MarketHub(mgr.real.fx, daemon_cfg.watch_symbols, store)
        _start_guard(mgr.demo)
        _start_guard(mgr.real)

        app = build_app(hub, mgr, store, daemon_cfg)
        logger.info("Web 服务: http://%s:%d/", args.host, port)
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
        return 0
    finally:
        if hub is not None:
            hub.close()
        if store is not None:
            store.close()
        mgr.stop()


if __name__ == "__main__":
    raise SystemExit(main())
