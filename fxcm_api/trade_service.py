"""交易服务：市价(+range)/限价入场/止损入场(客户端触发式)/平仓/改损/撤单 + 订单日志。

执行语义（已批复）：
- 入场单自动路由：触发价在市价有利侧 → 原生 LIMIT_ENTRY（服务器保证不劣化）；
  突破侧 → 方案 B 客户端触发（TriggerManager 监控价格进入 band → 市价单携带 range）。
- 市价单默认不带 range；可选 range_pips 以 RATE_MIN/RATE_MAX 实现（At Market Range）。
- 入场单默认 GTD 24h（EXPIRE_DATE_TIME，UTC）。
- 安全口径 N1：保证金 = amount × contract_size × price × MMR；安全手数 ≤ 净值 50%。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from forexconnect import ForexConnect, fxcorepy

from fxcm_api.pips import pip_size
from fxcm_api.trading import find_offer, resolve_account, trade_is_buy, wait_for_new_trade

logger = logging.getLogger("fxcm_api.trade_service")

TIF = fxcorepy.Constants.TIF

# 最小交易单位（demo 实测 + FXCM 标准；品种可按需扩充）
MIN_AMOUNTS: dict[str, int] = {"XAU/USD": 10, "USD/JPY": 1000, "EUR/USD": 1000}
# 合约与保证金估算元数据（MMR=保证金率；FXCM 各品种杠杆不同，此处为估值，可在配置覆盖）
CONTRACT_META: dict[str, dict] = {
    "XAU/USD": {"contract_size": 1.0, "mmr": 0.01},       # 100:1，amount 单位 = 盎司
    "USD/JPY": {"contract_size": 1.0, "mmr": 0.005},      # 200:1，amount 单位 = 基础货币
    "EUR/USD": {"contract_size": 1.0, "mmr": 0.005},
}
SAFE_MARGIN_RATIO = 0.5


def classify_entry(is_buy: bool, rate: float, bid: float, ask: float) -> str:
    """入场单自动路由：有利侧→limit（原生严格），突破侧→stop（客户端触发）。"""
    if is_buy:
        return "limit" if rate < ask else "stop"
    return "limit" if rate > bid else "stop"


def max_safe_amount(symbol: str, price: float, equity: float,
                    meta: dict | None = None) -> int:
    m = (meta or CONTRACT_META).get(symbol, {"contract_size": 1.0, "mmr": 0.01})
    per_unit_margin = m["contract_size"] * price * m["mmr"]
    if per_unit_margin <= 0:
        return 0
    return int(equity * SAFE_MARGIN_RATIO / per_unit_margin)


def trade_constraints(fx, symbol: str, equity: float,
                      pip_overrides: dict | None = None,
                      min_stop_distance_pips: float = 0.0) -> dict:
    offer = find_offer(fx, symbol)
    price = float(offer.ask)
    return {
        "symbol": symbol,
        "price": price,
        "pip_size": pip_size(symbol, float(offer.point_size), int(offer.digits), pip_overrides),
        "min_stop_distance_pips": min_stop_distance_pips,
        "min_amount": MIN_AMOUNTS.get(symbol, 1),
        "contract_size": CONTRACT_META.get(symbol, {}).get("contract_size", 1.0),
        "mmr": CONTRACT_META.get(symbol, {}).get("mmr", 0.01),
        "max_safe_amount": max_safe_amount(symbol, price, equity),
    }


def _pip(fx, symbol: str, pip_overrides: dict | None) -> float:
    offer = find_offer(fx, symbol)
    return pip_size(symbol, float(offer.point_size), int(offer.digits), pip_overrides)


def _journal(store, env: str, **kw) -> None:
    if store is not None:
        store.add_journal(env=env, **kw)


def market_open(fx, env: str, symbol: str, is_buy: bool, amount: int,
                range_pips: float | None = None, sl_pips: float | None = None,
                tp_pips: float | None = None, pip_overrides: dict | None = None,
                store=None, sl_price: float | None = None,
                tp_price: float | None = None) -> dict:
    """市价开仓（可选 range/附带止损/止盈），等待成交并记录订单日志。

    sl_price/tp_price 为绝对价格（优先于 *_pips 偏移语义）。"""
    offer = find_offer(fx, symbol)
    account = resolve_account(fx)
    pip = _pip(fx, symbol, pip_overrides)
    rate = float(offer.ask if is_buy else offer.bid)
    kwargs: dict = {
        "OFFER_ID": offer.offer_id, "ACCOUNT_ID": account, "AMOUNT": int(amount),
        "BUY_SELL": fxcorepy.Constants.BUY if is_buy else fxcorepy.Constants.SELL,
        "SYMBOL": symbol, "RATE": rate,
    }
    if range_pips and range_pips > 0:
        kwargs["ORDER_TYPE"] = fxcorepy.Constants.Orders.MARKET_OPEN_RANGE
        kwargs["RATE_MIN"] = round(rate - range_pips * pip, 10)
        kwargs["RATE_MAX"] = round(rate + range_pips * pip, 10)
    else:
        kwargs["ORDER_TYPE"] = fxcorepy.Constants.Orders.TRUE_MARKET_OPEN
    if sl_price:
        kwargs["RATE_STOP"] = round(float(sl_price), 10)
    elif sl_pips:
        kwargs["RATE_STOP"] = round(rate - sl_pips * pip, 10) if is_buy \
            else round(rate + sl_pips * pip, 10)
    if tp_price:
        kwargs["RATE_LIMIT"] = round(float(tp_price), 10)
    elif tp_pips:
        kwargs["RATE_LIMIT"] = round(rate + tp_pips * pip, 10) if is_buy \
            else round(rate - tp_pips * pip, 10)
    req = fx.create_order_request(
        order_type=kwargs.pop("ORDER_TYPE"),
        command=fxcorepy.Constants.Commands.CREATE_ORDER, **kwargs)
    known = {t.trade_id for t in fx.get_table(ForexConnect.TRADES)}
    fx.send_request(req)
    _journal(store, env, order_type="market", symbol=symbol,
             side="B" if is_buy else "S", amount=amount,
             requested_rate=rate, status="sent")
    trade = wait_for_new_trade(fx, offer.offer_id, known, timeout_s=10.0)
    if trade is not None:
        filled = float(trade.open_rate)
        _journal(store, env, order_type="market", symbol=symbol,
                 side="B" if is_buy else "S", amount=amount,
                 requested_rate=rate, filled_rate=filled, status="filled",
                 sl=kwargs.get("RATE_STOP"), tp=kwargs.get("RATE_LIMIT"),
                 detail=f"trade_id={trade.trade_id} open={filled}")
        return {"ok": True, "trade_id": trade.trade_id, "requested_rate": rate,
                "filled_rate": filled}
    _journal(store, env, order_type="market", symbol=symbol,
             side="B" if is_buy else "S", amount=amount,
             requested_rate=rate, status="unconfirmed")
    return {"ok": True, "trade_id": None, "requested_rate": rate,
            "note": "未在 10s 内确认成交，请查持仓"}


def place_limit_entry(fx, env: str, symbol: str, is_buy: bool, rate: float,
                      amount: int, gtd_hours: float = 24.0, sl_pips: float | None = None,
                      tp_pips: float | None = None, pip_overrides: dict | None = None,
                      store=None) -> dict:
    """原生限价入场单（服务器保证不劣于指定价成交），默认 GTD 24h。"""
    offer = find_offer(fx, symbol)
    account = resolve_account(fx)
    pip = _pip(fx, symbol, pip_overrides)
    expire = (datetime.now(tz=timezone.utc) + timedelta(hours=gtd_hours)) \
        .strftime("%Y%m%d-%H:%M:%S")
    kwargs: dict = {
        "OFFER_ID": offer.offer_id, "ACCOUNT_ID": account, "AMOUNT": int(amount),
        "BUY_SELL": fxcorepy.Constants.BUY if is_buy else fxcorepy.Constants.SELL,
        "SYMBOL": symbol, "RATE": rate,
        "TIME_IN_FORCE": str(TIF.GTD), "EXPIRE_DATE_TIME": expire,
    }
    if sl_pips:
        kwargs["RATE_STOP"] = round(rate - sl_pips * pip, 10) if is_buy \
            else round(rate + sl_pips * pip, 10)
    if tp_pips:
        kwargs["RATE_LIMIT"] = round(rate + tp_pips * pip, 10) if is_buy \
            else round(rate - tp_pips * pip, 10)
    req = fx.create_order_request(
        order_type=fxcorepy.Constants.Orders.LIMIT_ENTRY,
        command=fxcorepy.Constants.Commands.CREATE_ORDER, **kwargs)
    fx.send_request(req)
    _journal(store, env, order_type="limit_entry", symbol=symbol,
             side="B" if is_buy else "S", amount=amount, requested_rate=rate,
             status="working", sl=kwargs.get("RATE_STOP"),
             tp=kwargs.get("RATE_LIMIT"), detail=f"gtd={expire}")
    return {"ok": True, "order_type": "LIMIT_ENTRY", "rate": rate, "gtd": expire}


def entry_order(fx, env: str, symbol: str, is_buy: bool, rate: float, amount: int,
                band_pips: float = 10.0, gtd_hours: float = 24.0,
                pip_overrides: dict | None = None, store=None,
                trigger_manager=None) -> dict:
    """入场单自动路由（决定原生限价 or 客户端触发式止损入场）。"""
    offer = find_offer(fx, symbol)
    kind = classify_entry(is_buy, rate, float(offer.bid), float(offer.ask))
    if kind == "limit":
        r = place_limit_entry(fx, env, symbol, is_buy, rate, amount, gtd_hours,
                              pip_overrides=pip_overrides, store=store)
        r["routed"] = "limit"
        return r
    if trigger_manager is not None:
        tid = trigger_manager.register(env, symbol, is_buy, rate, amount,
                                       band_pips, gtd_hours)
        return {"ok": True, "routed": "stop_trigger", "trigger_id": tid,
                "rate": rate, "band_pips": band_pips}
    trigger_id = f"T-{int(time.time() * 1000)}"
    _journal(store, env, order_type="stop_entry_trigger", symbol=symbol,
             side="B" if is_buy else "S", amount=amount, requested_rate=rate,
             status="trigger_active", detail=f"trigger_id={trigger_id} "
             f"band_pips={band_pips} gtd_hours={gtd_hours}")
    return {"ok": True, "routed": "stop_trigger", "trigger_id": trigger_id,
            "rate": rate, "band_pips": band_pips}


def close_position(fx, env: str, trade_id: str, amount: int | None = None,
                   store=None) -> dict:
    from fxcm_api.trading import close_trade
    account = resolve_account(fx)
    close_trade(fx, account, trade_id, amount)
    _journal(store, env, order_type="close", symbol="", side="",
             amount=amount, requested_rate=None, status="sent",
             detail=f"trade_id={trade_id}")
    return {"ok": True, "trade_id": trade_id}


def modify_stop(fx, env: str, trade_id: str, new_sl: float,
                pip_overrides: dict | None = None, store=None) -> dict:
    """手动改损：与 guard 同路径（EDIT_ORDER），只影响指定持仓。"""
    trades = fx.get_table(ForexConnect.TRADES)
    row = next((t for t in trades if str(t.trade_id) == str(trade_id)), None)
    if row is None:
        return {"ok": False, "error": "持仓不存在"}
    stop_id = getattr(row, "stop_order_id", "") or ""
    offer_id = row.offer_id
    offers = fx.get_table(ForexConnect.OFFERS)
    symbol = next((o.instrument for o in offers if o.offer_id == offer_id), "")
    is_buy = trade_is_buy(row)
    kwargs: dict = {"OFFER_ID": offer_id, "ACCOUNT_ID": resolve_account(fx),
                    "RATE": float(new_sl), "TRADE_ID": str(trade_id)}
    if stop_id:
        kwargs["ORDER_ID"] = stop_id
        command = fxcorepy.Constants.Commands.EDIT_ORDER
    else:
        command = fxcorepy.Constants.Commands.CREATE_ORDER
        kwargs["BUY_SELL"] = fxcorepy.Constants.SELL if is_buy \
            else fxcorepy.Constants.BUY
        kwargs["AMOUNT"] = int(row.amount)
        kwargs["SYMBOL"] = symbol
    req = fx.create_order_request(order_type=fxcorepy.Constants.Orders.STOP,
                                  command=command, **kwargs)
    fx.send_request(req)
    _journal(store, env, order_type="modify_sl", symbol=symbol, side="",
             requested_rate=float(new_sl), status="sent", detail=f"trade_id={trade_id}")
    return {"ok": True, "trade_id": trade_id, "stop": float(new_sl)}


def modify_tp(fx, env: str, trade_id: str, new_tp: float,
              pip_overrides: dict | None = None, store=None) -> dict:
    """手动改止盈：已有 limit 挂单走 EDIT_ORDER，否则新挂 LIMIT 单。

    方向校验（资金安全）：多头止盈须高于 bid、空头须低于 ask——
    错误侧 LIMIT 单会立即触发市价平仓，客户端必须先行拦截。"""
    trades = fx.get_table(ForexConnect.TRADES)
    row = next((t for t in trades if str(t.trade_id) == str(trade_id)), None)
    if row is None:
        return {"ok": False, "error": "持仓不存在"}
    limit_id = getattr(row, "limit_order_id", "") or ""
    offer_id = row.offer_id
    offers = fx.get_table(ForexConnect.OFFERS)
    offer = next((o for o in offers if o.offer_id == offer_id), None)
    if offer is None:
        return {"ok": False, "error": "未找到该持仓的行情，无法校验止盈方向"}
    symbol = offer.instrument
    is_buy = trade_is_buy(row)
    if is_buy and float(new_tp) <= float(offer.bid):
        return {"ok": False, "error": f"多头止盈须高于现价（bid={offer.bid}）"}
    if not is_buy and float(new_tp) >= float(offer.ask):
        return {"ok": False, "error": f"空头止盈须低于现价（ask={offer.ask}）"}
    kwargs: dict = {"OFFER_ID": offer_id, "ACCOUNT_ID": resolve_account(fx),
                    "RATE": float(new_tp), "TRADE_ID": str(trade_id)}
    if limit_id:
        kwargs["ORDER_ID"] = limit_id
        command = fxcorepy.Constants.Commands.EDIT_ORDER
    else:
        command = fxcorepy.Constants.Commands.CREATE_ORDER
        kwargs["BUY_SELL"] = fxcorepy.Constants.SELL if is_buy \
            else fxcorepy.Constants.BUY
        kwargs["AMOUNT"] = int(row.amount)
        kwargs["SYMBOL"] = symbol
    req = fx.create_order_request(order_type=fxcorepy.Constants.Orders.LIMIT,
                                  command=command, **kwargs)
    fx.send_request(req)
    _journal(store, env, order_type="modify_tp", symbol=symbol, side="",
             requested_rate=float(new_tp), status="sent", detail=f"trade_id={trade_id}")
    return {"ok": True, "trade_id": trade_id, "limit": float(new_tp)}


def cancel_order(fx, env: str, order_id: str, store=None) -> dict:
    req = fx.create_order_request(
        order_type=fxcorepy.Constants.Orders.LIMIT_ENTRY,
        command=fxcorepy.Constants.Commands.DELETE_ORDER,
        ACCOUNT_ID=resolve_account(fx), ORDER_ID=str(order_id))
    fx.send_request(req)
    _journal(store, env, order_type="cancel", status="sent", detail=f"order_id={order_id}")
    return {"ok": True, "order_id": order_id}


def working_orders(fx) -> list[dict]:
    """未成交入场挂单快照（LE/SE/RLE/RSE）。"""
    out = []
    try:
        for o in fx.get_table(ForexConnect.ORDERS):
            otype = str(getattr(o, "type", ""))
            if otype not in ("LE", "SE", "RLE", "RSE"):
                continue
            out.append({
                "order_id": o.order_id, "type": otype, "offer_id": o.offer_id,
                "is_buy": getattr(o, "buy_sell", "B") == "B",
                "rate": float(o.rate), "amount": int(o.amount),
                "stage": str(getattr(o, "stage", "")),
            })
    except Exception as exc:
        logger.warning("ORDERS 表读取失败: %s", exc)
    return out


class TriggerManager:
    """方案 B：止损入场单的客户端触发。

    监控行情，价格进入 [触发价, 触发价+band]（买）或 [触发价-band, 触发价]（卖）时
    发市价单并携带 range=band 剩余空间（服务器强制范围内成交，否则拒单）。
    触发器经 order_journal 持久化（status=trigger_active），daemon 重启自动重挂未过期项。
    """

    def __init__(self, mgr, hub, store, pip_overrides: dict | None = None):
        import threading

        self.mgr = mgr
        self.hub = hub
        self.store = store
        self.pip_overrides = pip_overrides or {}
        self._triggers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()

    def register(self, env: str, symbol: str, is_buy: bool, rate: float,
                 amount: int, band_pips: float, gtd_hours: float) -> str:
        worker = self.mgr.worker(env)
        pip = _pip(worker.fx, symbol, self.pip_overrides)
        tid = f"T-{int(time.time() * 1000)}"
        trigger = {
            "trigger_id": tid, "env": env, "symbol": symbol, "is_buy": is_buy,
            "rate": float(rate), "amount": int(amount), "band_pips": float(band_pips),
            "pip": pip, "expire_ts": time.time() + gtd_hours * 3600,
            "status": "trigger_active",
        }
        with self._lock:
            self._triggers[tid] = trigger
        _journal(self.store, env, order_type="stop_entry_trigger", symbol=symbol,
                 side="B" if is_buy else "S", amount=amount, requested_rate=rate,
                 status="trigger_active",
                 detail=f"trigger_id={tid} band_pips={band_pips} "
                        f"gtd_hours={gtd_hours} pip={pip}")
        return tid

    def load_from_journal(self) -> int:
        import re

        restored = 0
        for row in self.store.get_journal(limit=1000):
            if row["order_type"] != "stop_entry_trigger" \
                    or row["status"] != "trigger_active":
                continue
            m = re.search(r"trigger_id=(\S+) band_pips=([\d.]+) gtd_hours=([\d.]+) "
                          r"pip=([\d.eE+-]+)", row["detail"] or "")
            if not m:
                continue
            tid, band, gtd_hours, pip = m.group(1), float(m.group(2)), \
                float(m.group(3)), float(m.group(4))
            expire_ts = row["ts"] + gtd_hours * 3600
            if time.time() >= expire_ts:
                continue
            self._triggers[tid] = {
                "trigger_id": tid, "env": row["env"], "symbol": row["symbol"],
                "is_buy": row["side"] == "B", "rate": float(row["requested_rate"]),
                "amount": int(row["amount"] or 0), "band_pips": band, "pip": pip,
                "expire_ts": expire_ts, "status": "trigger_active",
            }
            restored += 1
        if restored:
            logger.info("恢复 %d 个止损入场触发器", restored)
        return restored

    def cancel(self, trigger_id: str) -> bool:
        with self._lock:
            t = self._triggers.pop(trigger_id, None)
        if t is None:
            return False
        _journal(self.store, t["env"], order_type="stop_entry_trigger",
                 symbol=t["symbol"], status="trigger_cancelled",
                 detail=f"trigger_id={trigger_id}")
        return True

    def active(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._triggers.values()]

    def run_loop(self) -> None:
        while not self._stop_ev.is_set():
            time.sleep(0.5)
            with self._lock:
                items = list(self._triggers.items())
            for tid, t in items:
                now = time.time()
                if now >= t["expire_ts"]:
                    with self._lock:
                        self._triggers.pop(tid, None)
                    _journal(self.store, t["env"], order_type="stop_entry_trigger",
                             symbol=t["symbol"], status="trigger_expired",
                             detail=f"trigger_id={tid}")
                    continue
                tick = self.hub.last_tick(t["symbol"])
                if not tick:
                    continue
                bid, ask = tick[0], tick[1]
                price = ask if t["is_buy"] else bid
                upper = t["rate"] + t["band_pips"] * t["pip"]
                lower = t["rate"] - t["band_pips"] * t["pip"]
                hit = (lower <= price <= upper) if t["is_buy"] \
                    else (lower <= price <= upper)
                if not hit:
                    continue
                remaining = max((upper - price) / t["pip"], 0.5) if t["is_buy"] \
                    else max((price - lower) / t["pip"], 0.5)
                worker = self.mgr.worker(t["env"])
                if worker.fx is None:
                    continue
                try:
                    r = market_open(worker.fx, t["env"], t["symbol"], t["is_buy"],
                                    t["amount"], range_pips=remaining,
                                    pip_overrides=self.pip_overrides, store=self.store)
                    with self._lock:
                        self._triggers.pop(tid, None)
                    logger.info("触发式止损入场成交 %s %s: %s", t["symbol"],
                                "BUY" if t["is_buy"] else "SELL", r)
                    _journal(self.store, t["env"], order_type="stop_entry_trigger",
                             symbol=t["symbol"], status="triggered",
                             detail=f"trigger_id={tid} result={r}")
                except Exception as exc:
                    logger.warning("触发单发送失败（保持触发器）: %s", exc)
        self._stop_ev.clear()

    def stop(self) -> None:
        self._stop_ev.set()
