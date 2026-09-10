"""成本模型：往返成本以价格单位计（如 XAU 1.0 = $1.00），回测收益先扣成本再进统计。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """spread_rt=往返点差；slippage_side=单边滑点；commission_rate=比例佣金（按名义额，v1 默认 0）。"""

    spread_rt: float = 0.35
    slippage_side: float = 0.0
    commission_rate: float = 0.0

    @property
    def total_per_trade(self) -> float:
        return self.spread_rt + 2.0 * self.slippage_side
