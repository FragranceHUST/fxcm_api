"""market_open 绝对价格 TP/SL 覆盖逻辑测试（fx 用 Mock，不登录）。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fxcm_api import trade_service


def _make_fx():
    fx = MagicMock()
    fx.get_table.return_value = []
    offer = SimpleNamespace(offer_id="1", ask=4400.3, bid=4400.0,
                            point_size=0.01, digits=2)
    with patch.object(trade_service, "find_offer", return_value=offer), \
         patch.object(trade_service, "resolve_account", return_value="ACC"), \
         patch.object(trade_service, "wait_for_new_trade", return_value=None):
        yield fx


class TestMarketOpenPriceOverrides(unittest.TestCase):
    def _run(self, **kw):
        gen = _make_fx()          # 必须持有生成器引用，否则 patch 随 GC 撤销
        fx = next(gen)
        try:
            trade_service.market_open(fx, "demo", "XAU/USD", True, 10, **kw)
        finally:
            gen.close()
        return fx.create_order_request.call_args.kwargs

    def test_tp_price_overrides_pips(self):
        kw = self._run(tp_pips=10.0, tp_price=4412.34, pip_overrides={"XAU/USD": 0.1})
        self.assertEqual(kw["RATE_LIMIT"], 4412.34)

    def test_sl_price_overrides_pips(self):
        kw = self._run(sl_pips=10.0, sl_price=4390.5, pip_overrides={"XAU/USD": 0.1})
        self.assertEqual(kw["RATE_STOP"], 4390.5)

    def test_pips_semantics_unchanged_when_no_price(self):
        kw = self._run(sl_pips=150.0, tp_pips=100.0, pip_overrides={"XAU/USD": 0.1})
        self.assertEqual(kw["RATE_STOP"], 4400.3 - 15.0)   # ask 侧入场价 4400.3
        self.assertEqual(kw["RATE_LIMIT"], 4400.3 + 10.0)

    def test_neither_means_no_attached_orders(self):
        kw = self._run(pip_overrides={"XAU/USD": 0.1})
        self.assertNotIn("RATE_STOP", kw)
        self.assertNotIn("RATE_LIMIT", kw)


if __name__ == "__main__":
    unittest.main()
