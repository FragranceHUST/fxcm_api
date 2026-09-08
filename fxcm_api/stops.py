"""止损数学（纯函数，可离线单元测试）——对标 MQL4 版 ManageOrder/TryMoveSL。

级联优先级：保本(BE) > 移动止损(TRAIL) > 初始止损(INIT)，
最终候选必须满足"只向有利方向改善"，且距市价满足最小距离，否则返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass



@dataclass
class TradeSnap:
    trade_id: str
    offer_id: str
    symbol: str
    is_buy: bool
    amount: int
    open_rate: float
    stop_order_id: str | None   # None 或空串 = 尚无止损单
    current_stop: float | None = None   # 当前止损价（trade 行的 stop 字段）


@dataclass
class OfferSnap:
    offer_id: str
    symbol: str
    bid: float
    ask: float
    point_size: float
    digits: int


@dataclass
class StopCandidate:
    new_sl: float
    reason: str                    # INIT / BE / TRAIL


def has_stop(trade: TradeSnap) -> bool:
    return bool(trade.stop_order_id)


def initial_sl(open_rate: float, is_buy: bool, sl_pips: float, pip: float) -> float:
    return open_rate - sl_pips * pip if is_buy else open_rate + sl_pips * pip


def breakeven_sl(open_rate: float, is_buy: bool, buffer_pips: float, pip: float) -> float:
    return open_rate + buffer_pips * pip if is_buy else open_rate - buffer_pips * pip


def trailing_sl(close_price: float, is_buy: bool, dist_pips: float, pip: float) -> float:
    return close_price - dist_pips * pip if is_buy else close_price + dist_pips * pip


def is_better(new_sl: float, cur_sl: float | None, is_buy: bool,
              min_improve: float) -> bool:
    """多头止损只许上移、空头只许下移；cur_sl 为空视为可任意设置。"""
    if new_sl <= 0.0:
        return False
    if cur_sl is None or cur_sl <= 0.0:
        return True
    return (new_sl - cur_sl) > min_improve if is_buy else (cur_sl - new_sl) > min_improve


def clamp_to_market(new_sl: float, close_price: float, is_buy: bool,
                    min_dist: float) -> float:
    """新止损距平仓价（买用 Bid、卖用 Ask）必须至少 min_dist，超出则取最近合法值。"""
    if is_buy:
        return min(new_sl, close_price - min_dist)
    return max(new_sl, close_price + min_dist)


def profit_pips(trade: TradeSnap, offer: OfferSnap, pip: float) -> float:
    close_price = offer.bid if trade.is_buy else offer.ask
    diff = (close_price - trade.open_rate) if trade.is_buy else (trade.open_rate - close_price)
    return diff / pip


def evaluate_trade(trade: TradeSnap, offer: OfferSnap, pip: float,
                   *, initial_sl_pips: float, be_trigger_pips: float, be_buffer_pips: float,
                   use_trailing: bool, trail_start_pips: float, trail_dist_pips: float,
                   trail_step_pips: float, min_stop_distance_pips: float,
                   epsilon: float | None = None) -> StopCandidate | None:
    """对单笔持仓评估止损动作，返回候选或 None（无需修改）。"""
    if pip <= 0.0:
        return None

    eps = epsilon if epsilon is not None else offer.point_size * 0.5
    cur_sl = trade.current_stop
    pips = profit_pips(trade, offer, pip)
    close_price = offer.bid if trade.is_buy else offer.ask
    min_dist = min_stop_distance_pips * pip

    raw: float | None = None
    reason = ""
    min_improve = eps
    # 浮点报价差会在整数 pips 边界产生 4.9999... 噪声，触发比较必须带容差
    trigger_tol = 1e-9

    if use_trailing and trail_start_pips > 0 and pips >= trail_start_pips - trigger_tol:
        raw = trailing_sl(close_price, trade.is_buy, trail_dist_pips, pip)
        reason = "TRAIL"
        min_improve = max(eps, trail_step_pips * pip)
    elif be_trigger_pips > 0 and pips >= be_trigger_pips - trigger_tol:
        raw = breakeven_sl(trade.open_rate, trade.is_buy, be_buffer_pips, pip)
        reason = "BE"
    elif not has_stop(trade) and initial_sl_pips > 0:
        raw = initial_sl(trade.open_rate, trade.is_buy, initial_sl_pips, pip)
        reason = "INIT"

    if raw is None:
        return None

    candidate = clamp_to_market(raw, close_price, trade.is_buy, min_dist)
    candidate = round(candidate, offer.digits)
    if not is_better(candidate, cur_sl, trade.is_buy, min_improve):
        return None
    return StopCandidate(new_sl=candidate, reason=reason)
