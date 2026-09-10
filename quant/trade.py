"""Trade 基类：字段口径对齐 Fx_Quant_System/BackTest/source/Trade.h（批复 D1）。

方向沿用 C++ 枚举命名：Ask=买入方向（多），Bid=卖出方向（空）。
扩展字段（exit_reason/param_set_id/mfe/mae）用于回测试验追踪，不影响口径一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Direction(IntEnum):
    ASK = 1    # C++ DirectionType::Ask —— 多头
    BID = -1   # C++ DirectionType::Bid —— 空头


EXIT_REASONS = ("tp", "sl", "signal", "eod")


@dataclass
class Trade:
    trade_id: int
    instrument: str
    direction: Direction
    entry_price: float
    exit_price: float = 0.0
    stoploss_price: float = 0.0
    profit_target_price: float = 0.0
    quantity: int = 1
    entry_time: int = 0
    exit_time: int = 0
    commission: float = 0.0
    commission_rate: float = 0.0
    profit: float = 0.0            # 扣除往返成本后（价格单位 × 单位价值）
    real: bool = False
    closed: bool = False
    exit_reason: str = ""
    param_set_id: str = ""
    mfe: float = 0.0
    mae: float = 0.0

    @property
    def is_buy(self) -> bool:
        return self.direction == Direction.ASK

    @property
    def holding_seconds(self) -> int:
        return max(0, self.exit_time - self.entry_time)
