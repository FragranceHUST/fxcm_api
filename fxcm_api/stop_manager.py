"""止损管家运行时：轮询 TRADES/OFFERS 表，评估并下发止损修改请求。

运行约定：
  - 表格由 table manager 自动刷新，这里只负责按周期评估
  - 已有止损的持仓不会被覆盖初始值，但保本/移动逻辑仍可改善它
  - 发单失败不中断循环，下一轮自然重试（自愈）
  - 发单成功后 5 秒在途窗口：表格刷新有延迟，防止下一周期对同一持仓重复下单
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

PENDING_WINDOW_S = 5.0


def _is_duplicate_stop_error(exc: Exception) -> bool:
    text = str(exc)
    return "ORA-20114" in text or "more than one order of this type" in text


class StopManager:
    def __init__(self, fx: ForexConnect | None, settings: GuardSettings, fx_provider=None):
        self.fx = fx
        self.settings = settings
        self._fx_provider = fx_provider
        self.account_id: str = settings.account_id
        self._account_resolved = False
        self._seen_wrapper: object | None = None
        self._last_missing_log = 0.0
        self._last_dry_run: dict[str, tuple[str, float]] = {}
        self._pending: dict[str, float] = {}   # trade_id -> 在途窗口截止时间（monotonic）

    def _current_fx(self):
        """每周期解析当前 fx：daemon 会话重连后自动跟随新包装器。

        旧包装器在重连时会被置 _session=None，静态持有会导致 guard 永久失效
        （18k 条 NoneType 错误的根因），故 daemon 侧传入 fx_provider。
        """
        fx = self._fx_provider() if self._fx_provider is not None else self.fx
        if fx is None:
            now = time.monotonic()
            if now - self._last_missing_log > 30.0:
                logger.warning("会话未就绪（重连中），guard 本周期跳过")
                self._last_missing_log = now
            return None
        if fx is not self._seen_wrapper:
            self._seen_wrapper = fx
            self._account_resolved = False      # 新会话重新解析账户
        return fx

    def _resolve_account(self, fx) -> None:
        if self._account_resolved:
            return
        accounts = fx.get_table(ForexConnect.ACCOUNTS)
        if accounts is None or accounts.size == 0:
            return
        row = accounts.get_row(0)
        self.account_id = self.settings.account_id or str(row.account_id)
        self._account_resolved = True
        logger.info("管理账户: %s", self.account_id)

    def _snapshots(self, fx) -> list[tuple[TradeSnap, OfferSnap, float]]:
        offers: dict[str, OfferSnap] = {}
        for row in fx.get_table(ForexConnect.OFFERS):
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
        for row in fx.get_table(ForexConnect.TRADES):
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

    def _apply(self, fx, trade: TradeSnap, candidate_new_sl: float, reason: str) -> None:
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
            request = fx.create_order_request(
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
            request = fx.create_order_request(
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
        response = fx.send_request(request)
        logger.info("#%s %s %s 止损 -> %s (%s) 响应=%s",
                    trade.trade_id, trade.symbol,
                    "BUY" if trade.is_buy else "SELL",
                    candidate_new_sl, reason, repr(response))

    def run_cycle(self) -> int:
        fx = self._current_fx()
        if fx is None:
            return 0
        self._resolve_account(fx)
        if not self.account_id:
            logger.warning("账户表尚未就绪，本周期跳过")
            return 0

        acted = 0
        for trade, offer, pip in self._snapshots(fx):
            if self._pending.get(trade.trade_id, 0.0) > time.monotonic():
                continue   # 在途窗口内，等表格刷新反映上一次发单的结果
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
                self._apply(fx, trade, candidate.new_sl, candidate.reason)
                acted += 1
                self._pending[trade.trade_id] = time.monotonic() + PENDING_WINDOW_S
            except Exception as exc:
                if _is_duplicate_stop_error(exc):
                    # 服务器已存在同类型止损单（表格刷新延迟所致），请求等效于已生效
                    logger.info("#%s (%s) 服务器已存在止损单，本周期跳过: %.60s",
                                trade.trade_id, candidate.reason, exc)
                    self._pending[trade.trade_id] = time.monotonic() + PENDING_WINDOW_S
                else:
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
