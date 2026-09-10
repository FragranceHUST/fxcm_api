"""引擎内核场景测试：手工构造 m1 序列，逐条验证已批复的撮合语义。"""
from __future__ import annotations

import unittest

import numpy as np

from quant.data import OHLCV
from quant.engine import run


def make_data(bars: list[tuple[int, float, float, float, float]]) -> OHLCV:
    """bars: (ts_offset, open, high, low, close)，价格基于 100。"""
    ts = np.array([1_700_000_000 + int(b[0]) * 60 for b in bars], dtype=np.int64)
    return OHLCV(ts,
                 np.array([b[1] for b in bars]),
                 np.array([b[2] for b in bars]),
                 np.array([b[3] for b in bars]),
                 np.array([b[4] for b in bars]),
                 np.ones(len(bars), dtype=np.int64))


def flat_bands(n, upper=110.0, lower=100.0):
    return np.full(n, upper), np.full(n, lower)


class TestEngine(unittest.TestCase):
    def test_long_entry_cross_and_tp_touch(self):
        # band_lower=100：bar0 收 99（带下），bar1 收 100.5（穿回）→ 开多 @100
        # bar1 high=100.6 < tp=101 不出场；bar2 high=101.2 触 TP → 平 @101
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 100.5, 101.2, 100.4, 101.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "tp")
        self.assertAlmostEqual(t.entry_price, 100.0)
        self.assertAlmostEqual(t.exit_price, 101.0)
        self.assertAlmostEqual(t.profit, 1.0)

    def test_double_touch_same_bar_sl_wins(self):
        # 入场后当根同时触及 SL(99) 与 TP(101) → SL 优先（B2 保守）
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 101.2, 98.5, 100.5),   # 穿回入场，且 low≤SL、high≥TP
                (2, 100.0, 100.2, 99.8, 100.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "sl")
        self.assertAlmostEqual(trades[0].exit_price, 99.0)

    def test_breakeven_move_then_sl_at_cost(self):
        # be_trigger=0.5, be_buffer=0：bar1 high=100.6 触发保本 → sl 上移到 100
        # bar3 low=100.0 触及 → 平 @100，profit=-cost
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 100.5, 100.6, 100.2, 100.4),
                (3, 100.2, 100.3, 100.0, 100.1)]
        trades = run(make_data(bars), *flat_bands(4),
                     np.array([np.nan, 1.0, 1.0, 1.0]),
                     np.array([np.nan, 1.0, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3,
                     be_enabled=True, be_trigger=0.5, be_buffer=0.0,
                     cost_per_trade=0.2)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "be")
        self.assertAlmostEqual(t.exit_price, 100.0)
        self.assertAlmostEqual(t.profit, -0.2)     # 保本单仍承担成本

    def test_short_mirror(self):
        # band_upper=110：bar0 收 111（带上），bar1 收 109.5（向下穿回）→ 开空 @110
        # bar2 low=108.8 ≤ tp=109 → 平 @109
        bars = [(0, 111.0, 111.2, 110.8, 111.0),
                (1, 110.1, 110.2, 109.4, 109.5),
                (2, 109.5, 109.6, 108.8, 109.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.direction.value, -1)
        self.assertEqual(t.exit_reason, "tp")
        self.assertAlmostEqual(t.entry_price, 110.0)
        self.assertAlmostEqual(t.exit_price, 109.0)
        self.assertAlmostEqual(t.profit, 1.0)

    def test_single_position_ignores_signal_while_holding(self):
        # 持仓期间再出现穿越信号被忽略（D4 单持仓）；TP 平仓且收回到带下后，下根信号才生效
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),   # 入场 @100, tp=101, sl=99
                (2, 100.0, 101.2, 99.2, 99.5),   # 触 TP 平仓，收盘跌回带下
                (3, 99.5, 100.6, 99.4, 100.5),   # 再次穿回 → 第二笔
                (4, 100.5, 101.2, 100.4, 101.1)]
        trades = run(make_data(bars), *flat_bands(5),
                     np.array([np.nan, 1.0, 1.0, 1.0, 1.0]),
                     np.array([np.nan, 1.0, 1.0, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3, cost_per_trade=0.0)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[1].entry_time, 1_700_000_000 + 3 * 60)

    def test_cost_deducted(self):
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 100.5, 101.2, 100.4, 101.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="XAU/USD", direction_mode=3, cost_per_trade=0.35,
                     unit_value=1.0, quantity=10)
        self.assertAlmostEqual(trades[0].profit, (1.0 - 0.35) * 10)

    def test_direction_long_mode_blocks_short(self):
        # direction_mode=1（只多）：上沿穿回的空头信号不执行
        bars = [(0, 111.0, 111.2, 110.8, 111.0),
                (1, 110.1, 110.2, 109.4, 109.5)]
        trades = run(make_data(bars), *flat_bands(2),
                     np.array([np.nan, np.nan]), np.array([np.nan, np.nan]),
                     instrument="XAU/USD", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(trades, [])


if __name__ == "__main__":
    unittest.main()
