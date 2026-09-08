"""OrderGuard-Python：FXCM ForexConnect 版订单止损管家。

策略与 MQL4/Experts/OrderGuard.mq4 (V1) 一致：
  1. 初始止损：无止损的持仓自动附加固定点数止损
  2. 保本推损：浮盈达到触发点数后，将止损推至开仓价±缓冲
  3. 移动止损：可选（默认关闭），盈利扩大后按固定间距逐步上移
设计原则：只管理不开仓；止损只向有利方向移动，绝不放宽。
"""

from fxcm_api.config import Credentials, GuardSettings, load_config
from fxcm_api.stops import OfferSnap, StopCandidate, TradeSnap, evaluate_trade
from fxcm_api.pips import pip_size

__version__ = "0.1.0"

__all__ = [
    "Credentials",
    "GuardSettings",
    "load_config",
    "OfferSnap",
    "StopCandidate",
    "TradeSnap",
    "evaluate_trade",
    "pip_size",
    "__version__",
]
