"""quad runner 纯逻辑单测：信号判定 / 带与 TP-SL 计算 / 配置解析 / guard 归因。"""

from __future__ import annotations

import unittest

from fxcm_api.config import ArmConfig, GuardSettings, load_strategy_settings
from fxcm_api.stops import OfferSnap, TradeSnap, atr_from_candles, be_trigger_for
from fxcm_api.strategy_runner import band_prices, entry_signal, resample_h4, tp_sl_prices


class _Bar:
    def __init__(self, ts, open_, high, low, close):
        self.ts, self.open, self.high, self.low, self.close = ts, open_, high, low, close


class TestResampleH4(unittest.TestCase):
    def test_epoch_aligned_buckets(self):
        # given: 桶 0 内三根 m1 + 下一桶一根 m1（144s 桶）
        bars = [_Bar(0, 1.0, 1.2, 0.9, 1.1), _Bar(20, 1.1, 1.3, 1.0, 1.2),
                _Bar(40, 1.2, 1.25, 0.95, 1.05), _Bar(144, 1.05, 1.15, 1.0, 1.1)]
        out = resample_h4(bars, 144)
        self.assertEqual(len(out), 2)
        ts, o, h, l, c = out[0]
        self.assertEqual((ts, o, h, l, c), (0, 1.0, 1.3, 0.9, 1.05))
        self.assertEqual(out[1], (144, 1.05, 1.15, 1.0, 1.1))

    def test_atr_from_resampled_matches_engine_semantics(self):
        # given: 14 个连续 H4 桶，每桶 3 根 m1、桶内 range 0.004
        bars = [_Bar(b * 14400 + k * 60, 1.1, 1.102, 1.098, 1.1)
                for b in range(14) for k in range(3)]
        buckets = resample_h4(bars)
        self.assertEqual(len(buckets), 14)
        # 12 个 TR 需要 13 根已收桶（TR 用前桶 close），与 runner 切片同式
        atr = atr_from_candles(buckets[-14:-1], 12)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.003)   # 每桶 TR≈0.004，阈值须贴近量级


class TestBandPrices(unittest.TestCase):
    def test_symmetric_around_bucket_open(self):
        bl, bu = band_prices(1.1000, 0.0020, 0.5)
        self.assertAlmostEqual(bl, 1.0990)
        self.assertAlmostEqual(bu, 1.1010)

    def test_param1_scales_width(self):
        bl1, _ = band_prices(1.1000, 0.0020, 0.35)
        bl2, _ = band_prices(1.1000, 0.0020, 0.80)
        self.assertGreater(bl1, bl2)   # 带越宽，下沿越低


class TestEntrySignal(unittest.TestCase):
    def test_long_cross_back_inside(self):
        bl, bu = 1.0990, 1.1010
        self.assertTrue(entry_signal("long", 1.0985, 1.0992, bl, bu))
        self.assertFalse(entry_signal("long", 1.0995, 1.0992, bl, bu))   # 前收在带内
        self.assertFalse(entry_signal("long", 1.0985, 1.0988, bl, bu))   # 未收回带内

    def test_long_exact_touch_counts(self):
        bl, bu = 1.0990, 1.1010
        self.assertTrue(entry_signal("long", 1.0989, 1.0990, bl, bu))    # 收在带沿=带内

    def test_short_cross_back_inside(self):
        bl, bu = 1.0990, 1.1010
        self.assertTrue(entry_signal("short", 1.1015, 1.1008, bl, bu))
        self.assertFalse(entry_signal("short", 1.1005, 1.1008, bl, bu))
        self.assertFalse(entry_signal("short", 1.1015, 1.1012, bl, bu))

    def test_short_exact_touch_counts(self):
        bl, bu = 1.0990, 1.1010
        self.assertTrue(entry_signal("short", 1.1011, 1.1010, bl, bu))


class TestTpSlPrices(unittest.TestCase):
    def test_long_sides(self):
        sl, tp = tp_sl_prices(True, 1.1000, 0.0030, 0.0026, 5)
        self.assertAlmostEqual(sl, 1.0970)
        self.assertAlmostEqual(tp, 1.1026)

    def test_short_sides(self):
        sl, tp = tp_sl_prices(False, 1.1000, 0.0030, 0.0026, 5)
        self.assertAlmostEqual(sl, 1.1030)
        self.assertAlmostEqual(tp, 1.0974)

    def test_rounding_to_digits(self):
        sl, tp = tp_sl_prices(True, 1.100003, 0.00097, 0.00133, 4)
        self.assertEqual(sl, round(sl, 4))
        self.assertEqual(tp, round(tp, 4))


class TestStrategyConfig(unittest.TestCase):
    def test_load_arms_and_fields(self):
        cfg = load_strategy_settings("config.demo.json")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.symbol, "EUR/USD")
        self.assertEqual(cfg.quantity, 150000)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(len(cfg.arms), 4)
        f1s = next(a for a in cfg.arms if a.name == "F1S")
        self.assertEqual(f1s.direction, "short")
        self.assertEqual(f1s.be_pips, 8.0)
        self.assertEqual(cfg.arm_custom_id(f1s), "quad-F1S")

    def test_missing_file_returns_disabled(self):
        cfg = load_strategy_settings("nonexistent.json")
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.arms, [])


class TestBeTriggerFor(unittest.TestCase):
    @staticmethod
    def _trade(custom_id: str, is_buy: bool) -> TradeSnap:
        return TradeSnap(trade_id="1", offer_id="1", symbol="EUR/USD", is_buy=is_buy,
                         amount=150000, open_rate=1.1, stop_order_id=None,
                         custom_id=custom_id)

    def test_custom_id_wins_over_side(self):
        trade = self._trade("quad-F1S", is_buy=False)
        got = be_trigger_for(trade, 50.0, {"sell": 10.0}, {"quad-F1S": 8.0})
        self.assertEqual(got, 8.0)

    def test_falls_back_to_side(self):
        trade = self._trade("quad-F3S", is_buy=False)
        got = be_trigger_for(trade, 50.0, {"sell": 10.0}, {"quad-F1S": 8.0})
        self.assertEqual(got, 10.0)

    def test_manual_position_uses_global(self):
        trade = self._trade("", is_buy=True)
        got = be_trigger_for(trade, 50.0, None, {"quad-F1L": 10.0})
        self.assertEqual(got, 50.0)

    def test_guard_settings_default_none(self):
        self.assertIsNone(GuardSettings().be_trigger_pips_by_custom_id)


class TestArmConfigFromStrategy(unittest.TestCase):
    def test_arm_injection_mapping(self):
        cfg = load_strategy_settings("config.demo.json")
        mapping = {cfg.arm_custom_id(a): a.be_pips for a in cfg.arms}
        self.assertEqual(mapping["quad-F1S"], 8.0)
        self.assertEqual(mapping["quad-F3L"], 10.0)
        self.assertEqual(ArmConfig(name="X", direction="long", param1=0.5,
                                   tp_atr_mult=1.0, be_pips=5.0).param1, 0.5)


if __name__ == "__main__":
    unittest.main()
