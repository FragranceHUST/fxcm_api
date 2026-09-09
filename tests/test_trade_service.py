"""trade_service 纯逻辑单测：入场路由与安全手数。"""

import unittest

from fxcm_api.trade_service import classify_entry, max_safe_amount


class TestClassifyEntry(unittest.TestCase):
    def test_buy_below_market_is_limit(self):
        self.assertEqual(classify_entry(True, 4380.0, bid=4390.0, ask=4390.3), "limit")

    def test_buy_above_market_is_stop(self):
        self.assertEqual(classify_entry(True, 4420.0, bid=4390.0, ask=4390.3), "stop")

    def test_sell_above_market_is_limit(self):
        self.assertEqual(classify_entry(False, 4420.0, bid=4390.0, ask=4390.3), "limit")

    def test_sell_below_market_is_stop(self):
        self.assertEqual(classify_entry(False, 4380.0, bid=4390.0, ask=4390.3), "stop")

    def test_at_market_boundary(self):
        # 买入价 == ask → 突破侧（stop 触发式）
        self.assertEqual(classify_entry(True, 4390.3, bid=4390.0, ask=4390.3), "stop")


class TestSafeAmount(unittest.TestCase):
    def test_gold_margin_ratio(self):
        # XAU: 1 oz × 4409 × 0.01 ≈ $44.09/oz 保证金；50%×3756 → 42.5 → 42
        self.assertEqual(max_safe_amount("XAU/USD", 4409.0, 3756.63), 42)

    def test_zero_equity(self):
        self.assertEqual(max_safe_amount("XAU/USD", 4409.0, 0.0), 0)

    def test_unknown_symbol_uses_conservative_default(self):
        # 未知品种按 1% 保证金率：5000 / (1.26×0.01) = 396825 基础货币单位
        self.assertEqual(max_safe_amount("GBP/USD", 1.26, 10000.0), 396825)


if __name__ == "__main__":
    unittest.main()
