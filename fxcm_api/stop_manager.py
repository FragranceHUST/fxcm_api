"""止损管家运行时：轮询 TRADES/OFFERS 表，评估并下发止损修改请求。

对标 MQL4 版 ManageAllOrders/OnTimer(500ms)：
  - 表格由 table manager 自动刷新，这里只负责按周期评估
  - 已有止损的持仓不会被覆盖初始值，但保本/移动逻辑仍可改善它
  - 发单失败不中断循环，下一轮自然重试（自愈）
"""

from __future__ import annotations

import logging
import time

from forexconnect import ForexConnect, fxcorepy

from fxcm_api.config import GuardSettings
from fxcm_api.pips import pip_size
from fxcm_api.stops import OfferSnap, TradeSnap, evaluate_trade
from fxcm_api.trading import trade_is_buy

logger = logging.getLogger("fxcm_api.stop_manager")


class StopManager:
    def __init__(self, fx: ForexConnect, settings: GuardSettings):
        self.fx = fx
        self.settings = settings
        self.account_id: str = settings.account_id
        self._account_resolved = False
        self._last_dry_run: dict[str, tuple[str, float]] = {}

    def _resolve_account(self) -> None:
        if self._account_resolved:
            return
        accounts = self.fx.get_table(ForexConnect.ACCOUNTS)
        if accounts is None or accounts.size == 0:
            return
        row = accounts.get_row(0)
        self.account_id = self.settings.account_id or str(row.account_id)
        self._account_resolved = True
        logger.info("管理账户: %s", self.account_id)

    def _snapshots(self) -> list[tuple[TradeSnap, OfferSnap, float]]:
        offers: dict[str, OfferSnap] = {}
        for row in self.fx.get_table(ForexConnect.OFFERS):
            offer = OfferSnap(
                offer_id=row.offer_id,
                symbol=row.instrument,
                bid=row.bid,
                ask=row.ask,
                point_size=row.point_size,
                digits=int(row.digits),
            )
            offers[offer.offer_id] = offer

        result: list[tuple[TradeSnap, OfferSnap, float]] = []
        flt = self.settings.symbol_filter
        for row in self.fx.get_table(ForexConnect.TRADES):
            offer = offers.get(row.offer_id)
            if offer is None:
                continue
            if flt and offer.symbol not in flt:
                continue
            pip = pip_size(offer.symbol, offer.point_size, offer.digits,
                           self.settings.pip_overrides)
            if pip <= 0.0:
                logger.warning("品种 %s 点值数据为 0，跳过", offer.symbol)
                continue
            stop_id = getattr(row, "stop_order_id", "") or ""
            current_stop = getattr(row, "stop", 0.0) or 0.0
            trade = TradeSnap(
                trade_id=row.trade_id,
                offer_id=row.offer_id,
                symbol=offer.symbol,
                is_buy=trade_is_buy(row),
                amount=int(row.amount),
                open_rate=row.open_rate,
                stop_order_id=stop_id or None,
                current_stop=float(current_stop) if current_stop else None,
            )
            result.append((trade, offer, pip))
        return result

    def _apply(self, trade: TradeSnap, candidate_new_sl: float, reason: str) -> None:
        if self.settings.dry_run:
            current = (reason, candidate_new_sl)
            if self._last_dry_run.get(trade.trade_id) == current and not self.settings.verbose:
                return   # 同一动作连续周期不重复刷屏
            self._last_dry_run[trade.trade_id] = current
            logger.info("[dry_run] #%s %s %s 止损 -> %s (%s)（已拦截，未发请求）",
                        trade.trade_id, trade.symbol,
                        "BUY" if trade.is_buy else "SELL",
                        candidate_new_sl, reason)
            return

        if trade.stop_order_id:
            request = self.fx.create_order_request(
                order_type=fxcorepy.Constants.Orders.STOP,
                command=fxcorepy.Constants.Commands.EDIT_ORDER,
                OFFER_ID=trade.offer_id,
                ACCOUNT_ID=self.account_id,
                RATE=candidate_new_sl,
                TRADE_ID=trade.trade_id,
                ORDER_ID=trade.stop_order_id,
            )
        else:
            # 挂到持仓上的止损单方向与持仓相反（买仓的止损是卖方向 STOP 单）
            request = self.fx.create_order_request(
                order_type=fxcorepy.Constants.Orders.STOP,
                command=fxcorepy.Constants.Commands.CREATE_ORDER,
                OFFER_ID=trade.offer_id,
                ACCOUNT_ID=self.account_id,
                RATE=candidate_new_sl,
                TRADE_ID=trade.trade_id,
                BUY_SELL=fxcorepy.Constants.SELL if trade.is_buy else fxcorepy.Constants.BUY,
                AMOUNT=trade.amount,
                SYMBOL=trade.symbol,
            )
        response = self.fx.send_request(request)
        logger.info("#%s %s %s 止损 -> %s (%s) 响应=%s",
                    trade.trade_id, trade.symbol,
                    "BUY" if trade.is_buy else "SELL",
                    candidate_new_sl, reason, repr(response))

    def run_cycle(self) -> int:
        self._resolve_account()
        if not self.account_id:
            logger.warning("账户表尚未就绪，本周期跳过")
            return 0

        acted = 0
        for trade, offer, pip in self._snapshots():
            try:
                candidate = evaluate_trade(
                    trade, offer, pip,
                    initial_sl_pips=self.settings.initial_sl_pips,
                    be_trigger_pips=self.settings.be_trigger_pips,
                    be_buffer_pips=self.settings.be_buffer_pips,
                    use_trailing=self.settings.use_trailing,
                    trail_start_pips=self.settings.trail_start_pips,
                    trail_dist_pips=self.settings.trail_dist_pips,
                    trail_step_pips=self.settings.trail_step_pips,
                    min_stop_distance_pips=self.settings.min_stop_distance_pips,
                )
            except Exception:
                logger.exception("评估 #%s 失败", trade.trade_id)
                continue
            if candidate is None:
                continue
            try:
                self._apply(trade, candidate.new_sl, candidate.reason)
                acted += 1
            except Exception:
                logger.exception("下单 #%s (%s) 失败，下一周期重试",
                                 trade.trade_id, candidate.reason)
        return acted

    def run_forever(self) -> None:
        interval = max(self.settings.poll_interval_ms, 100) / 1000.0
        logger.info("止损管家启动：初始SL=%spips 保本触发=%spips 缓冲=%spips "
                    "移动止损=%s 周期=%sms 品种=%s dry_run=%s",
                    self.settings.initial_sl_pips, self.settings.be_trigger_pips,
                    self.settings.be_buffer_pips,
                    "开" if self.settings.use_trailing else "关",
                    int(interval * 1000),
                    self.settings.symbol_filter or "全账户",
                    self.settings.dry_run)
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("周期异常，继续")
            time.sleep(interval)
