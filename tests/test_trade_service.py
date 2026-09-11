"""trade_service 纯逻辑单测：入场路由、安全手数与手动改止盈。"""

import unittest

from forexconnect import fxcorepy

from fxcm_api.trade_service import classify_entry, max_safe_amount, modify_tp


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


class TestModifyTp(unittest.TestCase):
    """modify_tp 纯逻辑：EDIT/CREATE 分支、方向校验与错误路径（离线 Fake）。"""

    class FakeTable(list):
        @property
        def size(self):
            return len(self)

        def get_row(self, i):
            return self[i]

    @staticmethod
    def make_trade(trade_id="T1", limit_order_id="", buy_sell="B"):
        return type("TradeRow", (), {
            "trade_id": trade_id, "offer_id": "O1", "amount": 1000,
            "open_rate": 1.10000, "limit": 0.0, "limit_order_id": limit_order_id,
            "buy_sell": buy_sell,
        })()

    class FakeFx:
        def __init__(self, trades):
            self.calls = []
            offer = type("OfferRow", (), {
                "offer_id": "O1", "instrument": "EUR/USD",
                "bid": 1.10000, "ask": 1.10002,
            })()
            acc = type("AccRow", (), {"account_id": "ACC1"})()
            self._tables = {
                "accounts": TestModifyTp.FakeTable([acc]),
                "offers": TestModifyTp.FakeTable([offer]),
                "trades": TestModifyTp.FakeTable(trades),
            }

        def get_table(self, name):
            key = name.name.lower() if hasattr(name, "name") else str(name).lower()
            return self._tables[key]

        def create_order_request(self, **kwargs):
            self.calls.append(("create", kwargs))
            return "REQUEST"

        def send_request(self, request):
            self.calls.append(("send", request))
            return True

    def test_edit_existing_limit_order(self):
        fx = self.FakeFx([self.make_trade(limit_order_id="L1")])
        r = modify_tp(fx, "demo", "T1", 1.10500)
        self.assertTrue(r["ok"])
        self.assertEqual(r["trade_id"], "T1")
        self.assertEqual(r["limit"], 1.10500)
        creates = [c for c in fx.calls if c[0] == "create"]
        self.assertEqual(len(creates), 1)
        kw = creates[0][1]
        self.assertEqual(kw["order_type"], fxcorepy.Constants.Orders.LIMIT)
        self.assertEqual(kw["command"], fxcorepy.Constants.Commands.EDIT_ORDER)
        self.assertEqual(kw["ORDER_ID"], "L1")
        self.assertEqual(kw["RATE"], 1.10500)
        self.assertEqual(kw["TRADE_ID"], "T1")
        self.assertEqual([c[0] for c in fx.calls], ["create", "send"])

    def test_create_limit_when_missing(self):
        fx = self.FakeFx([self.make_trade(limit_order_id="", buy_sell="B")])
        r = modify_tp(fx, "demo", "T1", 1.10500)
        self.assertTrue(r["ok"])
        kw = [c for c in fx.calls if c[0] == "create"][0][1]
        self.assertEqual(kw["order_type"], fxcorepy.Constants.Orders.LIMIT)
        self.assertEqual(kw["command"], fxcorepy.Constants.Commands.CREATE_ORDER)
        self.assertEqual(kw["BUY_SELL"], fxcorepy.Constants.SELL)   # 与持仓方向相反
        self.assertEqual(kw["AMOUNT"], 1000)
        self.assertEqual(kw["SYMBOL"], "EUR/USD")
        self.assertEqual(kw["OFFER_ID"], "O1")
        self.assertNotIn("ORDER_ID", kw)
        self.assertEqual([c[0] for c in fx.calls], ["create", "send"])

    def test_missing_trade_rejected(self):
        fx = self.FakeFx([self.make_trade(trade_id="T1")])
        r = modify_tp(fx, "demo", "T9", 1.10500)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "持仓不存在")
        self.assertEqual(fx.calls, [])

    def test_long_tp_below_bid_rejected(self):
        fx = self.FakeFx([self.make_trade(buy_sell="B")])
        r = modify_tp(fx, "demo", "T1", 1.09900)   # 低于 bid 1.10000
        self.assertFalse(r["ok"])
        self.assertIn("止盈", r["error"])
        self.assertEqual(fx.calls, [])             # 未发任何请求

    def test_short_tp_above_ask_rejected(self):
        fx = self.FakeFx([self.make_trade(buy_sell="S")])
        r = modify_tp(fx, "demo", "T1", 1.10100)   # 高于 ask 1.10002
        self.assertFalse(r["ok"])
        self.assertIn("止盈", r["error"])
        self.assertEqual(fx.calls, [])


if __name__ == "__main__":
    unittest.main()
