"""quad 策略 runner：vol_reversal 实盘版（四臂，live 语义与 quant 引擎对齐）。

对齐的已批复引擎语义（quant/engine.py D2/B2/B3/D4）：
- 入场信号（m1 收线评估）：前收在带外、当前收穿回带内——
  long: prev_close < band_lower 且 cur_close >= band_lower；
  short: prev_close > band_upper 且 cur_close <= band_upper。
- 带：当前 H4 桶 open ± ATR(H4,12 严格已收桶) × param1（桶 j 开盘即可得，零前视）。
- 出场：市价单携带 RATE_STOP/RATE_LIMIT（距离 = 信号桶 ATR × 倍数，发单时锁定），
  同根双触服务器侧处理（SL 优先语义由挂单结构保证）；跳空由服务器按市价成交。
- 保本：guard 按 CUSTOM_ID 映射的 be_pips 推保本（单调只改善），与引擎 be 语义一致。
- 单向单持仓：每臂同一时刻最多 1 仓，CUSTOM_ID={prefix}-{arm} 归因，重启自动对账。

与回测的已知偏差（计入成本模型）：回测按带沿理想价成交，实盘按 m1 收线后市价成交，
差值 = 滑点/点差成本，正是成本 1.2-1.5× 校准的来源。
"""

from __future__ import annotations

import logging
import threading
import time

from forexconnect import ForexConnect

from fxcm_api.config import ArmConfig, StrategySettings
from fxcm_api.pips import pip_size
from fxcm_api.stops import atr_from_candles
from fxcm_api.trading import find_offer

logger = logging.getLogger("fxcm_api.strategy_runner")

H4_SEC = 14400
IN_FLIGHT_S = 25.0      # 发单后归因窗口：等 TRADES 表刷新，防止同一臂重复入场
RECONCILE_S = 120.0     # 周期性对账间隔（会话重连/表格延迟自愈）
M1_LIMIT = 3400         # 重采样用 m1 深度：≥13 根 H4 桶（48h）+ 余量


def resample_h4(bars, h4_sec: int = H4_SEC) -> list[tuple[int, float, float, float, float]]:
    """m1 → epoch 对齐 H4 桶 (ts, open, high, low, close)，升序。

    与 quant.engine 的 DataFeed.resample 同口径（ts//h4_sec*h4_sec）。
    库内 4h 表混有服务器原生桶对齐（回填产物），ATR 必须绕开它从 m1 重采样。"""
    out: list[tuple[int, float, float, float, float]] = []
    for b in bars:
        key = int(b.ts) // h4_sec * h4_sec
        if out and out[-1][0] == key:
            t = out[-1]
            out[-1] = (key, t[1], max(t[2], float(b.high)),
                       min(t[3], float(b.low)), float(b.close))
        else:
            out.append((key, float(b.open), float(b.high), float(b.low), float(b.close)))
    return out


def band_prices(bucket_open: float, atr: float, param1: float) -> tuple[float, float]:
    """带沿：bucket_open ∓ ATR × param1（与 vol_reversal.prepare 同式）。"""
    return bucket_open - atr * param1, bucket_open + atr * param1


def entry_signal(direction: str, prev_close: float, cur_close: float,
                 band_lower: float, band_upper: float) -> bool:
    """m1 收线穿越信号（引擎 long_sig/short_sig 的实盘等价式）。"""
    if direction == "long":
        return prev_close < band_lower <= cur_close
    return prev_close > band_upper >= cur_close


def tp_sl_prices(is_buy: bool, entry_ref: float, sl_dist: float, tp_dist: float,
                 digits: int) -> tuple[float, float]:
    """由入场参考价（买用 ask / 卖用 bid）与距离算 SL/TP 绝对价（按报价位数取整）。"""
    if is_buy:
        sl = entry_ref - sl_dist
        tp = entry_ref + tp_dist
    else:
        sl = entry_ref + sl_dist
        tp = entry_ref - tp_dist
    return round(sl, digits), round(tp, digits)


class StrategyRunner:
    """四臂策略线程：m1 收线检测信号 → 市价单（SL/TP 附带）→ guard 管保本。"""

    def __init__(self, mgr, hub, store, settings: StrategySettings,
                 pip_overrides: dict | None = None):
        self.mgr = mgr
        self.hub = hub
        self.store = store
        self.settings = settings
        self.pip_overrides = pip_overrides or {}
        self._stop_ev = threading.Event()
        self._lock = threading.Lock()
        self._last_eval_ts: int | None = None      # symbol 级：每根已收 m1 只评估一次
        self._last_reconcile = 0.0
        self._warned_no_custom_id = False
        self._state: dict[str, dict] = {
            arm.name: {"trade_id": None, "in_flight_until": 0.0, "last_signal": None}
            for arm in settings.arms
        }
        self._status: dict = {"enabled": settings.enabled, "dry_run": settings.dry_run,
                              "symbol": settings.symbol, "arms": {}}

    # ---- 状态与归因 ------------------------------------------------------

    def _open_by_custom_id(self, fx) -> dict[str, list[str]]:
        """TRADES 表按 CUSTOM_ID 分组的未平仓映射（含 custom_id 缺失告警）。"""
        out: dict[str, list[str]] = {}
        missing = 0
        total = 0
        for row in fx.get_table(ForexConnect.TRADES):
            total += 1
            cid = str(getattr(row, "custom_id", "") or "")
            if not cid:
                missing += 1
            out.setdefault(cid, []).append(str(row.trade_id))
        if total and missing == total and not self._warned_no_custom_id:
            self._warned_no_custom_id = True
            logger.warning("TRADES 行不含 custom_id，臂归因退化为内存+journal 口径")
        return out

    def reconcile(self, fx) -> None:
        """重启对账：CUSTOM_ID 优先，缺失时回落 journal 中最近的 quad 成交。"""
        by_cid = self._open_by_custom_id(fx)
        for arm in self.settings.arms:
            cid = self.settings.arm_custom_id(arm)
            st = self._state[arm.name]
            hits = by_cid.get(cid) or []
            if hits:
                st["trade_id"] = hits[0]
                continue
            if st["trade_id"]:
                st["trade_id"] = None if cid not in by_cid else st["trade_id"]
            # journal 回落：找最近一笔本臂成交且仍在持仓表
            try:
                for row in self.store.get_journal(env=self.settings.env, limit=500):
                    detail = row.get("detail") or ""
                    if f"arm={arm.name}" in detail and row.get("status") == "filled":
                        tid = _detail_trade_id(detail)
                        if tid and tid in by_cid.get("", []):
                            st["trade_id"] = tid
                        break
            except Exception:
                logger.exception("journal 对账失败（arm=%s）", arm.name)

    # ---- 主循环 ----------------------------------------------------------

    def run_forever(self) -> None:
        s = self.settings
        logger.info("quad 策略 runner 启动：%s %d 臂 × %d units，SL=%.2f×ATR，dry_run=%s",
                    s.symbol, len(s.arms), s.quantity, s.sl_atr_mult, s.dry_run)
        while not self._stop_ev.is_set():
            try:
                self.run_cycle()
            except Exception:
                logger.exception("quad 周期异常，继续")
            self._stop_ev.wait(max(s.poll_interval_s, 0.5))

    def stop(self) -> None:
        self._stop_ev.set()

    def run_cycle(self) -> int:
        s = self.settings
        if not s.enabled or not s.arms:
            return 0
        worker = self.mgr.worker(s.env)
        if worker.fx is None or not worker.is_ready():
            return 0
        if not worker.daemon_cfg.allow_trading:
            return 0
        now = time.monotonic()
        if now - self._last_reconcile > RECONCILE_S:
            self._last_reconcile = now
            self.reconcile(worker.fx)

        bars = self.hub.candles(s.symbol, 60, limit=4)
        if len(bars) < 4:
            return 0
        closed = bars[:-1]                     # 最后一根为形成中 m1
        cur_bar, prev_bar = closed[-1], closed[-2]
        if cur_bar.ts == self._last_eval_ts:
            return 0
        if cur_bar.ts - prev_bar.ts != 60:
            # 数据缺口（断线/停机）：前收过期，跳过本根防伪信号
            self._last_eval_ts = cur_bar.ts
            logger.warning("m1 数据缺口（%s → %s），本根跳过信号评估",
                           prev_bar.ts, cur_bar.ts)
            return 0

        h4 = resample_h4(self.hub.candles(s.symbol, 60, limit=M1_LIMIT))
        if len(h4) < s.atr_period + 2:
            self._last_eval_ts = cur_bar.ts
            logger.warning("H4 桶不足（%d/%d），跳过（m1 历史累积中）",
                           len(h4), s.atr_period + 2)
            return 0
        forming_h4_ts, forming_h4_open = h4[-1][0], h4[-1][1]
        if cur_bar.ts // H4_SEC * H4_SEC != forming_h4_ts:
            self._last_eval_ts = cur_bar.ts
            return 0                           # 桶不一致（数据异常），保守跳过
        atr = atr_from_candles(h4[-(s.atr_period + 2):-1], s.atr_period)
        if atr is None or atr <= 0:
            self._last_eval_ts = cur_bar.ts
            return 0

        offer = find_offer(worker.fx, s.symbol)
        pip = pip_size(s.symbol, float(offer.point_size), int(offer.digits),
                       self.pip_overrides)
        if pip <= 0:
            return 0
        digits = int(offer.digits)
        open_map = self._open_by_custom_id(worker.fx)

        acted = 0
        per_arm_status: dict[str, dict] = {}
        for arm in s.arms:
            cid = s.arm_custom_id(arm)
            st = self._state[arm.name]
            entry: dict = {"direction": arm.direction, "param1": arm.param1,
                           "trade_id": st["trade_id"]}
            hits = open_map.get(cid) or []
            if hits:
                st["trade_id"] = hits[0]
                entry["trade_id"] = hits[0]
                entry["holding"] = True
                per_arm_status[arm.name] = entry
                continue
            bl, bu = band_prices(forming_h4_open, atr, arm.param1)
            sig = entry_signal(arm.direction, float(prev_bar.close),
                               float(cur_bar.close), bl, bu)
            entry.update({"prev_close": float(prev_bar.close),
                          "cur_close": float(cur_bar.close),
                          "band_lower": round(bl, digits), "band_upper": round(bu, digits),
                          "signal": sig})
            if not sig:
                per_arm_status[arm.name] = entry
                continue
            if now < st["in_flight_until"]:
                entry["in_flight"] = True
                per_arm_status[arm.name] = entry
                continue
            entry["fired"] = self._enter(worker, arm, atr, digits=digits, cid=cid)
            acted += 1
            per_arm_status[arm.name] = entry
        with self._lock:
            self._status.update({
                "enabled": True, "dry_run": s.dry_run, "symbol": s.symbol,
                "quantity": s.quantity, "sl_atr_mult": s.sl_atr_mult,
                "atr": round(atr, digits), "atr_pips": round(atr / pip, 1),
                "h4_bucket_open": forming_h4_open,
                "bucket_ts": int(forming_h4_ts),
                "h4_buckets": len(h4),
                "last_eval_ts": int(cur_bar.ts),
                "arms": per_arm_status,
            })
        self._last_eval_ts = cur_bar.ts
        return acted

    def _enter(self, worker, arm: ArmConfig, atr: float,
               digits: int, cid: str) -> bool:
        """发市价单（SL/TP 附带）；dry_run 只记录。返回是否已处理（含 dry_run）。"""
        from fxcm_api.trade_service import market_open

        s = self.settings
        tick = self.hub.last_tick(s.symbol)
        if tick:
            bid, ask = float(tick[0]), float(tick[1])
        else:
            offer = find_offer(worker.fx, s.symbol)
            bid, ask = float(offer.bid), float(offer.ask)
        is_buy = arm.direction == "long"
        entry_ref = ask if is_buy else bid
        sl_price, tp_price = tp_sl_prices(is_buy, entry_ref,
                                          s.sl_atr_mult * atr, arm.tp_atr_mult * atr,
                                          digits)
        detail = (f"arm={arm.name} atr={atr:.5f} sig_bar_ref={entry_ref:.5f} "
                  f"sl={sl_price:.5f} tp={tp_price:.5f}")
        if s.dry_run:
            logger.info("[quad][dry_run] %s %s %.0fk 触发：SL %.5f / TP %.5f（已拦截）",
                        arm.name, arm.direction, s.quantity / 1000, sl_price, tp_price)
            self._journal(is_buy, "dry_run_signal", detail)
            return False
        try:
            r = market_open(worker.fx, s.env, s.symbol, is_buy, s.quantity,
                            sl_price=sl_price, tp_price=tp_price,
                            pip_overrides=self.pip_overrides, store=self.store,
                            custom_id=cid)
            st = self._state[arm.name]
            st["in_flight_until"] = time.monotonic() + IN_FLIGHT_S
            if r.get("trade_id"):
                st["trade_id"] = str(r["trade_id"])
            logger.info("[quad] %s %s 入场：trade=%s ref=%.5f SL=%.5f TP=%.5f",
                        arm.name, arm.direction, r.get("trade_id"),
                        entry_ref, sl_price, tp_price)
            self._journal(is_buy, "filled" if r.get("trade_id") else "unconfirmed",
                          f"{detail} trade_id={r.get('trade_id')}")
            return True
        except Exception:
            logger.exception("[quad] %s 下单失败（下周期信号若仍在会重试）", arm.name)
            self._state[arm.name]["in_flight_until"] = time.monotonic() + 15.0
            self._journal(is_buy, "error", detail)
            return False

    def _journal(self, is_buy: bool, status: str, detail: str) -> None:
        if self.store is None:
            return
        try:
            self.store.add_journal(
                env=self.settings.env, order_type="quad_entry", symbol=self.settings.symbol,
                side="B" if is_buy else "S", amount=self.settings.quantity,
                requested_rate=None, status=status, detail=detail)
        except Exception:
            logger.exception("quad journal 写入失败")

    def status(self) -> dict:
        with self._lock:
            import copy
            return copy.deepcopy(self._status)


def _detail_trade_id(detail: str) -> str | None:
    marker = "trade_id="
    idx = detail.find(marker)
    if idx < 0:
        return None
    rest = detail[idx + len(marker):]
    tid = rest.split()[0].strip() if rest.split() else ""
    return tid or None
