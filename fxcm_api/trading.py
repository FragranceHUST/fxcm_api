"""验证工具箱：市价开仓/平仓/手动止损，用于 demo 上验证完整交易链路。

所有函数都走 ForexConnect 请求通道；发单后通过 table manager 自动推送的
TRADES 表轮询确认结果（无需额外订阅）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from forexconnect import ForexConnect, fxcorepy

logger = logging.getLogger("fxcm_api.trading")

CUSTOM_ID = "orderguard-validation"


def trade_is_buy(row) -> bool:
    """TRADES 行没有 is_buy 属性，方向字段是 buy_sell（'B'/'S'）。"""
    return row.buy_sell == fxcorepy.Constants.BUY


def resolve_account(fx, account_id: str = "") -> str:
    accounts = fx.get_table(ForexConnect.ACCOUNTS)
    if accounts is None or accounts.size == 0:
        raise RuntimeError("账户表为空")
    if account_id:
        return account_id
    return str(accounts.get_row(0).account_id)


def find_offer(fx, symbol: str):
    for row in fx.get_table(ForexConnect.OFFERS):
        if row.instrument == symbol:
            return row
    raise RuntimeError(f"未找到品种 {symbol}（FXCM 格式如 EUR/USD，注意大小写与斜杠）")


def open_market(fx, account_id: str, symbol: str, is_buy: bool, amount: int) -> None:
    request = fx.create_order_request(
        order_type=fxcorepy.Constants.Orders.TRUE_MARKET_OPEN,
        ACCOUNT_ID=account_id,
        SYMBOL=symbol,
        BUY_SELL=fxcorepy.Constants.BUY if is_buy else fxcorepy.Constants.SELL,
        AMOUNT=amount,
        CUSTOM_ID=CUSTOM_ID,
    )
    response = fx.send_request(request)
    logger.info("开仓请求已发送，响应=%s", repr(response))


def close_trade(fx, account_id: str, trade_id: str, amount: int) -> None:
    request = fx.create_order_request(
        order_type=fxcorepy.Constants.Orders.TRUE_MARKET_CLOSE,
        ACCOUNT_ID=account_id,
        TRADE_ID=trade_id,
        AMOUNT=amount,
        CUSTOM_ID=CUSTOM_ID,
    )
    response = fx.send_request(request)
    logger.info("平仓请求已发送，响应=%s", repr(response))


def wait_for_new_trade(fx, offer_id: str, known_ids: set[str],
                       timeout_s: float = 6.0) -> Optional[object]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for row in fx.get_table(ForexConnect.TRADES):
            if row.trade_id not in known_ids and row.offer_id == offer_id:
                return row
        time.sleep(0.4)
    return None


def wait_trade_gone(fx, trade_id: str, timeout_s: float = 6.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        table = fx.get_table(ForexConnect.TRADES)
        if all(row.trade_id != trade_id for row in table):
            return True
        time.sleep(0.4)
    return False
