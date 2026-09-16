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


class TestEntryFillPrice(unittest.TestCase):
    def test_band_touched_fills_at_band_edge(self):
        # bar1 low=99.95 触及 bl=100 → 按带沿理想价 100 成交（B3）
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 100.1, 100.6, 99.95, 100.5),
                (2, 100.5, 101.2, 100.4, 101.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].entry_price, 100.0)

    def test_gap_over_band_fills_at_open(self):
        # bar1 开盘 100.3 已在带上方（low 也不触及）→ 按开盘 100.3 成交
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 100.3, 100.6, 100.3, 100.5),
                (2, 100.6, 101.5, 100.4, 101.2)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].entry_price, 100.3)
        self.assertAlmostEqual(trades[0].exit_price, 101.3)


class TestExitFillPrice(unittest.TestCase):
    def test_sl_touched_fills_at_sl(self):
        # bar2 low=99.0 触及 sl=99、开盘仍在 SL 上方 → 按理想 SL 价成交
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 100.2, 100.3, 99.0, 99.8)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "sl")
        self.assertAlmostEqual(t.exit_price, 99.0)

    def test_gap_open_below_sl_fills_at_open(self):
        # bar2 开盘 98.5 已跳空越过 sl=99（周末/假日缺口）→ 按开盘 98.5 成交，不按 99 美化
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 98.5, 100.2, 98.0, 99.5)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "sl")
        self.assertAlmostEqual(t.exit_price, 98.5)

    def test_short_gap_open_above_sl_fills_at_open(self):
        # 空头镜像：bar2 开盘 111.6 越过 sl=111 → 按开盘成交
        bars = [(0, 111.0, 111.2, 110.8, 111.0),
                (1, 109.9, 110.05, 109.8, 109.5),
                (2, 111.6, 111.8, 110.9, 111.0)]
        trades = run(make_data(bars), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=2, cost_per_trade=0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "sl")
        self.assertAlmostEqual(t.entry_price, 110.0)
        self.assertAlmostEqual(t.exit_price, 111.6)


class TestUnitValueArr(unittest.TestCase):
    BARS = [(0, 99.0, 99.2, 98.8, 99.0),
            (1, 99.9, 100.6, 99.9, 100.5),
            (2, 100.5, 101.2, 100.4, 101.0)]

    def test_profit_uses_exit_bar_unit_value(self):
        # 出场 bar（i=2）uv=0.7：profit = (raw−cost) × quantity × uv[exit_i]
        trades = run(make_data(self.BARS), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.35,
                     quantity=10, unit_value_arr=np.array([0.5, 0.6, 0.7]))
        self.assertAlmostEqual(trades[0].profit, (1.0 - 0.35) * 10 * 0.7)

    def test_arr_wins_over_constant(self):
        trades = run(make_data(self.BARS), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.35,
                     quantity=10, unit_value=1.0,
                     unit_value_arr=np.array([0.5, 0.6, 0.7]))
        self.assertAlmostEqual(trades[0].profit, (1.0 - 0.35) * 10 * 0.7)

    def test_constant_fallback_without_arr(self):
        trades = run(make_data(self.BARS), *flat_bands(3),
                     np.array([np.nan, 1.0, 1.0]), np.array([np.nan, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.35,
                     quantity=10, unit_value=0.7)
        self.assertAlmostEqual(trades[0].profit, (1.0 - 0.35) * 10 * 0.7)


class TestBandNaNIdle(unittest.TestCase):
    def test_nan_band_zero_trades(self):
        # 带全 NaN（预热期）→ 引擎空转，即使 tp/sl 有效也不开仓
        bars = [(0, 99.0, 99.2, 98.8, 99.0),
                (1, 99.9, 100.6, 99.9, 100.5),
                (2, 100.5, 101.2, 100.4, 101.0)]
        trades = run(make_data(bars),
                     np.full(3, np.nan), np.full(3, np.nan),
                     np.array([1.0, 1.0, 1.0]), np.array([1.0, 1.0, 1.0]),
                     instrument="USD/JPY", direction_mode=1, cost_per_trade=0.0)
        self.assertEqual(trades, [])


if __name__ == "__main__":
    unittest.main()


class TestExcursions(unittest.TestCase):
    """mfe/mae 语义钉死：持有期内相对入场价的最大有利/不利偏移（价格单位，含入场 bar）。"""

    def test_sl_loser_mfe_mae(self):
        # bar1 穿回带内开多@100（tp=+0.10, sl=-0.04）；bar2 冲高 100.05（mfe）回落 99.99；
        # bar3 探 99.95 击穿 SL@99.96 → mfe=0.05, mae=0.05（含入场 bar 的 h/l）
        bars = [(0, 99.95, 99.97, 99.90, 99.90),
                (1, 99.98, 100.02, 99.97, 100.05),   # prev_c=99.90<100, c=100.05 → 开多@100
                (2, 100.03, 100.05, 99.99, 100.00),
                (3, 99.97, 99.99, 99.95, 99.95)]     # l=99.95 ≤ sl=99.96 → SL
        trades = run(make_data(bars), *flat_bands(4, 110.0, 100.0),
                     tp_dist=np.full(4, 0.10), sl_dist=np.full(4, 0.04),
                     instrument="EUR/USD", direction_mode=1,
                     cost_per_trade=0.0, quantity=50000)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "sl")
        self.assertAlmostEqual(t.mfe, 0.05, places=9)
        self.assertAlmostEqual(t.mae, 0.05, places=9)

    def test_tp_winner_with_adverse_dip(self):
        # 开多@100 后 bar2 探 99.97（mae=0.03）未触 SL，bar3 冲 100.15 触 TP@100.10
        bars = [(0, 99.95, 99.97, 99.90, 99.90),
                (1, 99.98, 100.02, 99.97, 100.05),
                (2, 100.03, 100.05, 99.97, 100.00),
                (3, 100.05, 100.15, 100.00, 100.12)]
        trades = run(make_data(bars), *flat_bands(4, 110.0, 100.0),
                     tp_dist=np.full(4, 0.10), sl_dist=np.full(4, 0.04),
                     instrument="EUR/USD", direction_mode=1,
                     cost_per_trade=0.0, quantity=50000)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.exit_reason, "tp")
        self.assertAlmostEqual(t.mfe, 0.15, places=9)
        self.assertAlmostEqual(t.mae, 0.03, places=9)
