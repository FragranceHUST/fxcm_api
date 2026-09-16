"""vol_reversal 策略数学测试：ATR 严格先桶、带映射、预热 NaN、PreloadedFeed 切片（全离线）。"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np

from quant.cost import CostModel
from quant.data import OHLCV, PreloadedFeed

_SPEC = importlib.util.spec_from_file_location(
    "vol_reversal", Path(__file__).resolve().parents[1] / "strategies" / "vol_reversal.py")
if _SPEC is None or _SPEC.loader is None:
    raise SystemExit("无法加载策略模块 strategies/vol_reversal.py")
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["vol_reversal"] = _mod
_SPEC.loader.exec_module(_mod)

BASE = (1_600_000_000 // 14_400) * 14_400     # H4 对齐起点
PER_BUCKET = 4


def synth_ohlcv(bucket_opens: list[float], mutate=None) -> OHLCV:
    """每桶 4 根 m1：桶内平推 o=c=p，h=p+0.05，l=p−0.05；mutate(b, k, bar) 可改写某根。"""
    rows = []
    for b, p in enumerate(bucket_opens):
        for k in range(PER_BUCKET):
            bar = {"ts": BASE + b * 14_400 + k * 60, "o": p,
                   "h": p + 0.05, "l": p - 0.05, "c": p}
            if mutate is not None:
                mutate(b, k, bar)
            rows.append(bar)
    ts = np.array([r["ts"] for r in rows], dtype=np.int64)
    return OHLCV(ts, np.array([r["o"] for r in rows]), np.array([r["h"] for r in rows]),
                 np.array([r["l"] for r in rows]), np.array([r["c"] for r in rows]),
                 np.ones(len(rows), dtype=np.int64))


def make_strategy(p1: float = 0.5):
    return _mod.build_strategy(params={"param1": p1}, symbol="USD/JPY",
                               cost_model=CostModel(spread_rt=0.01))


def make_strategy_dm(p1: float, direction_mode: str):
    return _mod.build_strategy(params={"param1": p1}, symbol="USD/JPY",
                               direction_mode=direction_mode,
                               cost_model=CostModel(spread_rt=0.01))


def step_opens(n: int) -> list[float]:
    return [100.0 + 0.1 * b for b in range(n)]


class TestContract(unittest.TestCase):
    def test_build_strategy_defaults(self):
        s = _mod.build_strategy(params={"param1": 0.5}, symbol="USD/JPY")
        self.assertEqual(s.type, "vol_reversal")
        self.assertEqual(s.direction_mode, "long")
        self.assertEqual(s.quantity, 50000)
        self.assertEqual(s.unit_value, 1.0)

    def test_tp_sl_constants_and_unit_value_arr(self):
        opens = step_opens(14)
        prep = make_strategy().prepare(PreloadedFeed(synth_ohlcv(opens)),
                                       BASE, BASE + 14 * 14_400)
        np.testing.assert_allclose(prep["tp_dist"], 45.0 * 0.01)
        np.testing.assert_allclose(prep["sl_dist"], 30.0 * 0.01)
        np.testing.assert_allclose(prep["unit_value_arr"],
                                   1.0 / prep["data"].close)
        np.testing.assert_array_equal(prep["band_upper"], prep["band_lower"])


class TestAtrPriorBuckets(unittest.TestCase):
    def test_atr_nan_before_period(self):
        opens = step_opens(14)
        prep = make_strategy().prepare(PreloadedFeed(synth_ohlcv(opens)),
                                       BASE, BASE + 14 * 14_400)
        bl = prep["band_lower"]
        self.assertTrue(np.isnan(bl[:12 * PER_BUCKET]).all())
        self.assertFalse(np.isnan(bl[12 * PER_BUCKET:]).any())

    def test_band_value_uses_prior_bucket_mean(self):
        # TR[0]=h−l=0.1，TR[j≥1]=0.15 → atr[12]=(0.1+11×0.15)/12
        opens = step_opens(14)
        prep = make_strategy().prepare(PreloadedFeed(synth_ohlcv(opens)),
                                       BASE, BASE + 14 * 14_400)
        bl = prep["band_lower"]
        atr12 = (0.1 + 11 * 0.15) / 12
        expected = 101.2 - atr12 * 0.5
        np.testing.assert_allclose(bl[12 * PER_BUCKET:13 * PER_BUCKET], expected)
        np.testing.assert_allclose(bl[13 * PER_BUCKET:], 101.3 - 0.15 * 0.5)

    def test_perturb_bucket_j_keeps_band_j_changes_band_j_plus_1(self):
        # 扰动桶 12 的最高价（末根 m1）：TR[12] 变 → atr[13] 变；atr[12]=mean(TR[0:12]) 不含 TR[12]
        opens = step_opens(14)
        base_bl = make_strategy().prepare(
            PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)["band_lower"]

        def bump(b, k, bar):
            if b == 12 and k == PER_BUCKET - 1:
                bar["h"] += 0.5

        pert_bl = make_strategy().prepare(
            PreloadedFeed(synth_ohlcv(opens, mutate=bump)),
            BASE, BASE + 14 * 14_400)["band_lower"]
        np.testing.assert_array_equal(pert_bl[12 * PER_BUCKET:13 * PER_BUCKET],
                                      base_bl[12 * PER_BUCKET:13 * PER_BUCKET])
        self.assertFalse(np.allclose(pert_bl[13 * PER_BUCKET:],
                                     base_bl[13 * PER_BUCKET:]))


class TestWarmup(unittest.TestCase):
    def test_band_nan_before_start_ts(self):
        opens = step_opens(14)
        start = BASE + 5 * 14_400
        prep = make_strategy().prepare(PreloadedFeed(synth_ohlcv(opens)),
                                       start, BASE + 14 * 14_400)
        bl, ts = prep["band_lower"], prep["data"].ts
        self.assertTrue(np.isnan(bl[ts < start]).all())
        self.assertFalse(np.isnan(bl[ts >= start]).all())

    def test_all_nan_band_zero_trades_despite_wild_swings(self):
        # 仅 10 桶 → ATR 永不定义 → 带全 NaN：价格剧烈振荡也不开仓（预热空转）
        opens = [100.0 if b % 2 == 0 else 105.0 for b in range(10)]

        def wild(b, k, bar):
            bar["h"] = bar["o"] + 2.0
            bar["l"] = bar["o"] - 2.0

        result = make_strategy().run_backtest(PreloadedFeed(synth_ohlcv(opens, mutate=wild)),
                                              BASE, BASE + 10 * 14_400)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.stats["closed_trade_cnt"], 0)


class TestBothModeBands(unittest.TestCase):
    def test_both_mode_bands_mirror(self):
        # 双带真实值：lower = open − atr×p1，upper = open + atr×p1（ATR 先桶口径同源）
        opens = step_opens(14)
        prep = make_strategy_dm(0.5, "both").prepare(
            PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)
        bu, bl = prep["band_upper"], prep["band_lower"]
        atr12 = (0.1 + 11 * 0.15) / 12
        np.testing.assert_allclose(bl[12 * PER_BUCKET:13 * PER_BUCKET],
                                   101.2 - atr12 * 0.5)
        np.testing.assert_allclose(bu[12 * PER_BUCKET:13 * PER_BUCKET],
                                   101.2 + atr12 * 0.5)
        np.testing.assert_allclose(bl[13 * PER_BUCKET:], 101.3 - 0.15 * 0.5)
        np.testing.assert_allclose(bu[13 * PER_BUCKET:], 101.3 + 0.15 * 0.5)

    def test_both_mode_upper_nan_warmup_rules(self):
        # upper 与 lower 同 NaN 规则：j < atr_period 为 NaN；ts < start_ts 为 NaN
        opens = step_opens(14)
        start = BASE + 5 * 14_400
        prep = make_strategy_dm(0.5, "both").prepare(
            PreloadedFeed(synth_ohlcv(opens)), start, BASE + 14 * 14_400)
        bu, bl, ts = prep["band_upper"], prep["band_lower"], prep["data"].ts
        self.assertTrue(np.isnan(bu[:12 * PER_BUCKET]).all())
        self.assertTrue(np.isnan(bu[ts < start]).all())
        self.assertFalse(np.isnan(bu[ts >= start]).all())
        self.assertTrue(np.isnan(bl[:12 * PER_BUCKET]).all())

    def test_short_mode_lower_is_upper_copy(self):
        # short 只读上沿：lower = upper 拷贝（内核 NaN 检查要求非 NaN）
        opens = step_opens(14)
        prep = make_strategy_dm(0.5, "short").prepare(
            PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)
        bu, bl = prep["band_upper"], prep["band_lower"]
        np.testing.assert_array_equal(bl, bu)
        atr12 = (0.1 + 11 * 0.15) / 12
        np.testing.assert_allclose(bu[12 * PER_BUCKET:13 * PER_BUCKET],
                                   101.2 + atr12 * 0.5)

    def test_long_mode_upper_still_lower_copy(self):
        opens = step_opens(14)
        prep = make_strategy_dm(0.5, "long").prepare(
            PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)
        np.testing.assert_array_equal(prep["band_upper"], prep["band_lower"])

    def test_both_mode_end_to_end_short_trade(self):
        # 空头端到端：下行步进序列，桶 12 首根 prev_c(100.9) > bu(100.873) 且 c 穿回 → 开空 @100.8
        opens = [102.0 - 0.1 * b for b in range(14)]
        result = make_strategy_dm(0.5, "short").run_backtest(
            PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertEqual(t.exit_reason, "eod")
        self.assertAlmostEqual(t.entry_price, 100.8)
        self.assertEqual(t.direction.value, -1)


class TestEndToEndPnl(unittest.TestCase):
    def test_single_trade_eod_profit_uses_exit_uv(self):
        # 桶 12 开盘穿回：带 101.127 低于该 m1 low(101.15) → 跳过带沿按开盘 101.2 成交；
        # 数据末尾按收盘 101.3 强平（eod）；profit = (exit−entry−0.01) × 50000 × (1/exit)
        opens = step_opens(14)
        result = make_strategy().run_backtest(PreloadedFeed(synth_ohlcv(opens)),
                                              BASE, BASE + 14 * 14_400)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertEqual(t.exit_reason, "eod")
        self.assertAlmostEqual(t.entry_price, 101.2)
        expected_profit = (101.3 - 101.2 - 0.01) * 50000 * (1.0 / 101.3)
        self.assertAlmostEqual(t.profit, expected_profit)


class TestVolExitModes(unittest.TestCase):
    """V2：tp_atr_mult / sl_atr_mult 可独立启用，未启用的侧保持固定点数。"""

    ATR12 = (0.1 + 11 * 0.15) / 12

    def _prep(self, params: dict):
        opens = step_opens(14)
        s = _mod.build_strategy(params={"param1": 0.5, **params}, symbol="USD/JPY",
                                cost_model=CostModel(spread_rt=0.01))
        return s.prepare(PreloadedFeed(synth_ohlcv(opens)), BASE, BASE + 14 * 14_400)

    def test_tp_only_vol_mode(self):
        prep = self._prep({"tp_atr_mult": 1.21})
        tp = prep["tp_dist"]
        self.assertTrue(np.isnan(tp[:12 * PER_BUCKET]).all())      # ATR 未定义 → NaN → 不开仓
        np.testing.assert_allclose(tp[12 * PER_BUCKET:13 * PER_BUCKET], self.ATR12 * 1.21)
        np.testing.assert_allclose(prep["sl_dist"], 30.0 * 0.01)

    def test_sl_only_vol_mode(self):
        prep = self._prep({"sl_atr_mult": 0.8})
        sl = prep["sl_dist"]
        self.assertTrue(np.isnan(sl[:12 * PER_BUCKET]).all())
        np.testing.assert_allclose(sl[12 * PER_BUCKET:13 * PER_BUCKET], self.ATR12 * 0.8)
        np.testing.assert_allclose(sl[13 * PER_BUCKET:], 0.15 * 0.8)
        np.testing.assert_allclose(prep["tp_dist"], 45.0 * 0.01)

    def test_both_vol_mode_unchanged(self):
        prep = self._prep({"tp_atr_mult": 1.21, "sl_atr_mult": 0.8})
        np.testing.assert_allclose(prep["tp_dist"][12 * PER_BUCKET:13 * PER_BUCKET],
                                   self.ATR12 * 1.21)
        np.testing.assert_allclose(prep["sl_dist"][12 * PER_BUCKET:13 * PER_BUCKET],
                                   self.ATR12 * 0.8)

    def test_tp_only_end_to_end_still_trades(self):
        # 桶 12 穿回入场 @101.2；vol TP=atr12×1.21≈0.176 未触及 → 期末 eod 平仓（与固定 TP 同路径）
        opens = step_opens(14)
        s = _mod.build_strategy(params={"param1": 0.5, "tp_atr_mult": 1.21},
                                symbol="USD/JPY", cost_model=CostModel(spread_rt=0.01))
        result = s.run_backtest(PreloadedFeed(synth_ohlcv(opens)),
                                BASE, BASE + 14 * 14_400)
        self.assertEqual(len(result.trades), 1)
        t = result.trades[0]
        self.assertEqual(t.exit_reason, "eod")
        self.assertAlmostEqual(t.entry_price, 101.2)
        self.assertAlmostEqual(t.profit_target_price, 101.2 + self.ATR12 * 1.21)


class TestPreloadedFeed(unittest.TestCase):
    def setUp(self):
        opens = step_opens(3)
        self.feed = PreloadedFeed(synth_ohlcv(opens))

    def test_full_load_returns_all(self):
        got = self.feed.load("USD/JPY", 60)
        self.assertEqual(len(got), 3 * PER_BUCKET)

    def test_range_bounds_inclusive(self):
        lo, hi = int(self.feed._ohlcv.ts[2]), int(self.feed._ohlcv.ts[9])
        got = self.feed.load("USD/JPY", 60, lo, hi)
        self.assertEqual(len(got), 8)
        self.assertEqual(int(got.ts[0]), lo)
        self.assertEqual(int(got.ts[-1]), hi)

    def test_empty_range(self):
        got = self.feed.load("USD/JPY", 60, BASE + 999_999, BASE + 1_999_999)
        self.assertEqual(len(got), 0)

    def test_slices_are_views_of_cache(self):
        got = self.feed.load("USD/JPY", 60, int(self.feed._ohlcv.ts[1]))
        self.assertIs(got.ts.base, self.feed._ohlcv.ts)


if __name__ == "__main__":
    unittest.main()
