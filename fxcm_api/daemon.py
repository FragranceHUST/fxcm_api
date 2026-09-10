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
from fxcm_api.data import backfill as backfill_module
from fxcm_api.data.hub import MarketHub
from fxcm_api.data.stats import compute_stats
from fxcm_api.data.store import CandleStore
from fxcm_api.sessions import SessionManager, SessionWorker
from fxcm_api.stop_manager import StopManager
from fxcm_api.trade_service import (
    TriggerManager,
    cancel_order,
    close_position,
    entry_order,
    market_open,
    modify_stop,
    trade_constraints,
    working_orders,
)
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
              daemon_cfg: DaemonSettings, tm: TriggerManager) -> FastAPI:
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

    @app.post("/api/{env}/orders")
    def place_order(env: str, body: dict):
        if env not in ("real", "demo"):
            raise HTTPException(404, f"未知环境: {env}")
        worker = mgr.worker(env)
        if worker.fx is None:
            raise HTTPException(503, f"{env} 会话未就绪")
        if not worker.daemon_cfg.allow_trading:
            raise HTTPException(403, f"{env} 环境已禁用交易（allow_trading=false）")
        if env == "real" and not body.get("confirm"):
            raise HTTPException(428, "真实环境下单需要 confirm=true（二次确认）")
        symbol = body.get("symbol", "")
        is_buy = bool(body.get("is_buy"))
        try:
            amount = int(body["amount"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "amount 缺失或非法")
        order_type = body.get("order_type", "market")
        pip_overrides = worker.guard.pip_overrides
        try:
            if order_type == "market":
                return market_open(worker.fx, env, symbol, is_buy, amount,
                                   range_pips=body.get("range_pips"),
                                   sl_pips=body.get("sl_pips"),
                                   tp_pips=body.get("tp_pips"),
                                   pip_overrides=pip_overrides, store=store,
                                   sl_price=body.get("sl_price"),
                                   tp_price=body.get("tp_price"))
            if order_type in ("limit", "stop"):
                return entry_order(worker.fx, env, symbol, is_buy,
                                   float(body["rate"]), amount,
                                   band_pips=float(body.get("band_pips", 10.0)),
                                   gtd_hours=float(body.get("gtd_hours", 24.0)),
                                   pip_overrides=pip_overrides, store=store,
                                   trigger_manager=tm)
            raise HTTPException(400, "order_type 须为 market/limit/stop")
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("[%s] 下单失败", env)
            raise HTTPException(500, str(exc)[:200])

    @app.get("/api/{env}/orders")
    def list_orders(env: str):
        if env not in ("real", "demo"):
            raise HTTPException(404, f"未知环境: {env}")
        worker = mgr.worker(env)
        return {"orders": working_orders(worker.fx) if worker.fx else [],
                "triggers": [t for t in tm.active() if t["env"] == env]}

    @app.delete("/api/{env}/orders/{order_id}")
    def remove_order(env: str, order_id: str):
        worker = mgr.worker(env)
        if worker.fx is None:
            raise HTTPException(503, f"{env} 会话未就绪")
        return cancel_order(worker.fx, env, order_id, store=store)

    @app.delete("/api/{env}/triggers/{trigger_id}")
    def remove_trigger(env: str, trigger_id: str):
        return {"ok": tm.cancel(trigger_id)}

    @app.post("/api/{env}/positions/{trade_id}/close")
    def close_pos(env: str, trade_id: str, body: dict | None = None):
        worker = mgr.worker(env)
        if worker.fx is None:
            raise HTTPException(503, f"{env} 会话未就绪")
        if env == "real" and not (body or {}).get("confirm"):
            raise HTTPException(428, "真实环境平仓需要 confirm=true（二次确认）")
        return close_position(worker.fx, env, trade_id,
                              amount=(body or {}).get("amount"), store=store)

    @app.patch("/api/{env}/positions/{trade_id}/sl")
    def patch_sl(env: str, trade_id: str, body: dict):
        worker = mgr.worker(env)
        if worker.fx is None:
            raise HTTPException(503, f"{env} 会话未就绪")
        if env == "real" and not body.get("confirm"):
            raise HTTPException(428, "真实环境改损需要 confirm=true（二次确认）")
        if body.get("price") is None:
            raise HTTPException(400, "需要 price（绝对止损价）")
        return modify_stop(worker.fx, env, trade_id, float(body["price"]),
                           pip_overrides=worker.guard.pip_overrides, store=store)

    @app.get("/api/{env}/trade-constraints")
    def constraints(env: str, symbol: str = "XAU/USD"):
        worker = mgr.worker(env)
        acct = worker.account_summary()
        if worker.fx is None or not acct:
            raise HTTPException(503, f"{env} 会话未就绪")
        return trade_constraints(worker.fx, symbol, acct["equity"],
                                 pip_overrides=worker.guard.pip_overrides,
                                 min_stop_distance_pips=worker.guard.min_stop_distance_pips)

    @app.get("/api/{env}/history/trades")
    def history_trades(env: str, limit: int = 200):
        if env not in ("real", "demo"):
            raise HTTPException(404, f"未知环境: {env}")
        return _closed_trades(mgr.worker(env).fx, limit=max(1, min(limit, 2000)))

    @app.get("/api/{env}/history/orders")
    def history_orders(env: str, limit: int = 200):
        return store.get_journal(env=env, limit=max(1, min(limit, 2000)))

    @app.get("/api/{env}/history/messages")
    def history_messages(env: str, limit: int = 50):
        fx = mgr.worker(env).fx
        if fx is None:
            return []
        msgs = fx.get_table(ForexConnect.MESSAGES)
        out = []
        for i, m in enumerate(list(msgs or [])[:max(1, min(limit, 200))]):
            out.append({"time": str(getattr(m, "time", "")),
                        "text": str(getattr(m, "text", ""))})
        return out

    @app.get("/api/{env}/stats")
    def env_stats(env: str):
        worker = mgr.worker(env)
        if worker.fx is None:
            raise HTTPException(503, f"{env} 会话未就绪")
        closed = _closed_trades(worker.fx, limit=2000)
        open_trades = _positions_snapshot(worker.fx)
        equity_curve = store.get_equity(env)
        journal = store.get_journal(env=env, limit=1000)
        return compute_stats(closed_trades=closed, open_trades=open_trades,
                             equity_curve=equity_curve, journal=journal)

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


def _equity_sampler(mgr: SessionManager, store: CandleStore) -> None:
    while True:
        for env in ("real", "demo"):
            acct = mgr.worker(env).account_summary()
            if acct:
                store.add_equity_sample(env, acct["balance"], acct["equity"],
                                        acct.get("margin_used", 0.0))
        time.sleep(30)


def _closed_trades(fx, limit: int = 500) -> list[dict]:
    if fx is None:
        return []
    offers = {row.offer_id: row.instrument for row in fx.get_table(ForexConnect.OFFERS)}
    closed = fx.get_table(ForexConnect.CLOSED_TRADES)
    out = []
    for row in list(closed or [])[:limit]:
        out.append({
            "trade_id": row.trade_id,
            "symbol": offers.get(row.offer_id, "?"),
            "is_buy": trade_is_buy(row),
            "amount": row.amount,
            "open_rate": row.open_rate,
            "close_rate": row.close_rate,
            "gross_pl": row.gross_pl,
            "open_time": getattr(row, "open_time", None),
            "close_time": getattr(row, "close_time", None),
            "commission": float(getattr(row, "commission", 0.0) or 0.0),
        })
    return out


def _startup_backfill(mgr: SessionManager, store: CandleStore, symbols: list[str], days: float) -> None:
    """重启补洞：复用 real 会话走快照分页通道（fx.get_history 的 pricearchive 通道本网络不可用，勿用）。"""
    if days <= 0:
        return
    fx = mgr.real.fx
    tf_labels = [lbl for lbl, sec in TF_LABELS.items() if sec in (60, 900, 3600, 14400, 86400)]
    for tf_label in tf_labels:
        for sym in symbols:
            try:
                r = backfill_module.backfill(fx, sym, tf_label, days / 365.0, store, delay_ms=250)
                logger.info("启动补洞 %s %s: +%s 根", sym, tf_label, r["bars"])
            except Exception as exc:
                logger.warning("启动补洞 %s %s 失败（超长缺口由回填脚本兜底）: %s", sym, tf_label, exc)


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
    tm = None
    try:
        store = CandleStore(Path(daemon_cfg.data_dir) / "candles.db")
        hub = MarketHub(mgr.real.fx, daemon_cfg.watch_symbols, store)
        _start_guard(mgr.demo)
        _start_guard(mgr.real)

        tm = TriggerManager(mgr, hub, store,
                            pip_overrides=mgr.demo.guard.pip_overrides)
        tm.load_from_journal()
        threading.Thread(target=tm.run_loop, name="triggers", daemon=True).start()
        threading.Thread(target=_equity_sampler, args=(mgr, store),
                         name="equity-sampler", daemon=True).start()
        if daemon_cfg.startup_backfill_days > 0:
            threading.Thread(target=_startup_backfill,
                             args=(mgr, store, daemon_cfg.watch_symbols,
                                   daemon_cfg.startup_backfill_days),
                             name="startup-backfill", daemon=True).start()

        app = build_app(hub, mgr, store, daemon_cfg, tm)
        logger.info("Web 服务: http://%s:%d/", args.host, port)
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
        return 0
    finally:
        if tm is not None:
            tm.stop()
        if hub is not None:
            hub.close()
        if store is not None:
            store.close()
        mgr.stop()


if __name__ == "__main__":
    raise SystemExit(main())
