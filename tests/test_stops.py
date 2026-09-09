"""止损数学离线单元测试（不依赖 forexconnect、不登录）。"""

from __future__ import annotations

import unittest

from fxcm_api.stops import (
    OfferSnap,
    TradeSnap,
    breakeven_sl,
    clamp_to_market,
    evaluate_trade,
    initial_sl,
    is_better,
    trailing_sl,
)

PIP = 0.0001
POINT = 0.00001
DIGITS = 5


def make_offer(bid: float = 1.10000, ask: float = 1.10002) -> OfferSnap:
    return OfferSnap(offer_id="1", symbol="EUR/USD", bid=bid, ask=ask,
                     point_size=POINT, digits=DIGITS)


def make_trade(open_rate: float = 1.10000, is_buy: bool = True,
               stop_order_id: str | None = None,
               current_stop: float | None = None) -> TradeSnap:
    return TradeSnap(trade_id="T1", offer_id="1", symbol="EUR/USD", is_buy=is_buy,
                     amount=1000, open_rate=open_rate, stop_order_id=stop_order_id,
                     current_stop=current_stop)


def evaluate(trade: TradeSnap, offer: OfferSnap,
             **overrides: object) -> StopCandidate | None:
    kwargs = dict(initial_sl_pips=20.0, be_trigger_pips=5.0, be_buffer_pips=1.0,
                  use_trailing=False, trail_start_pips=10.0, trail_dist_pips=8.0,
                  trail_step_pips=1.0, min_stop_distance_pips=0.5)
    kwargs.update(overrides)
    return evaluate_trade(trade, offer, PIP, **kwargs)


class TestPipMath(unittest.TestCase):
    def test_initial_sl_directions(self):
        self.assertAlmostEqual(initial_sl(1.10000, True, 20.0, PIP), 1.09800)
        self.assertAlmostEqual(initial_sl(1.10000, False, 20.0, PIP), 1.10200)

    def test_breakeven_directions(self):
        self.assertAlmostEqual(breakeven_sl(1.10000, True, 1.0, PIP), 1.10010)
        self.assertAlmostEqual(breakeven_sl(1.10000, False, 1.0, PIP), 1.09990)

    def test_trailing_directions(self):
        self.assertAlmostEqual(trailing_sl(1.10000, True, 8.0, PIP), 1.09920)
        self.assertAlmostEqual(trailing_sl(1.10000, False, 8.0, PIP), 1.10080)


class TestIsBetter(unittest.TestCase):
    def test_buy_only_up(self):
        self.assertTrue(is_better(1.1002, 1.1001, True, 0.0))
        self.assertFalse(is_better(1.1000, 1.1001, True, 0.0))
        self.assertTrue(is_better(1.1002, None, True, 0.0))

    def test_sell_only_down(self):
        self.assertTrue(is_better(1.1000, 1.1001, False, 0.0))
        self.assertFalse(is_better(1.1002, 1.1001, False, 0.0))

    def test_min_improve_epsilon(self):
        self.assertFalse(is_better(1.10015, 1.10010, True, 0.0001))
        self.assertTrue(is_better(1.10025, 1.10010, True, 0.0001))


class TestClamp(unittest.TestCase):
    def test_buy_clamped_below_bid(self):
        self.assertAlmostEqual(clamp_to_market(1.1050, 1.1000, True, 0.0005), 1.09950)

    def test_buy_unclamped_when_far(self):
        self.assertAlmostEqual(clamp_to_market(1.0900, 1.1000, True, 0.0005), 1.0900)

    def test_sell_clamped_above_ask(self):
        self.assertAlmostEqual(clamp_to_market(1.0900, 1.1000, False, 0.0005), 1.10050)


class TestEvaluateCascade(unittest.TestCase):
    def test_no_stop_and_losing_sets_initial(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id=None)
        offer = make_offer(bid=1.09950, ask=1.09952)   # 浮亏
        cand = evaluate(trade, offer)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "INIT")
        self.assertAlmostEqual(cand.new_sl, 1.09800)

    def test_profit_reaches_trigger_moves_breakeven(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1")
        offer = make_offer(bid=1.10060, ask=1.10062)   # 浮盈 6 pips
        cand = evaluate(trade, offer)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "BE")
        self.assertAlmostEqual(cand.new_sl, 1.10010)   # 开仓价 + 1 pip

    def test_profit_below_trigger_keeps_existing_stop(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1")
        offer = make_offer(bid=1.10020, ask=1.10022)   # 浮盈 2 pips
        self.assertIsNone(evaluate(trade, offer))

    def test_never_relax_better_existing_stop(self):
        # 已有止损 1.10050（优于 BE 目标 1.10010），浮盈刚到触发点：不得向下放宽
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1",
                           current_stop=1.10050)
        offer = make_offer(bid=1.10050, ask=1.10052)
        self.assertIsNone(evaluate(trade, offer))

    def test_improves_worse_existing_stop_to_breakeven(self):
        # 已有初始止损 1.09800，浮盈到触发点：应上移到 BE
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1",
                           current_stop=1.09800)
        offer = make_offer(bid=1.10050, ask=1.10052)
        cand = evaluate(trade, offer)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "BE")
        self.assertAlmostEqual(cand.new_sl, 1.10010)

    def test_sell_breakeven(self):
        trade = make_trade(open_rate=1.10000, is_buy=False, stop_order_id="S1")
        offer = make_offer(bid=1.09935, ask=1.09937)   # 卖方浮盈 ~6.3 pips（用 Ask 评估）
        cand = evaluate(trade, offer)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "BE")
        self.assertAlmostEqual(cand.new_sl, 1.09990)   # 开仓价 - 1 pip

    def test_trailing_engages_beyond_be(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1")
        offer = make_offer(bid=1.10120, ask=1.10122)   # 浮盈 12 pips
        cand = evaluate(trade, offer, use_trailing=True)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "TRAIL")
        self.assertAlmostEqual(cand.new_sl, 1.10040)   # bid - 8 pips

    def test_trailing_disabled_keeps_be_only(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1")
        offer = make_offer(bid=1.10120, ask=1.10122)
        cand = evaluate(trade, offer, use_trailing=False)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.reason, "BE")

    def test_min_distance_prevents_too_close_stop(self):
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id=None)
        offer = make_offer(bid=1.09900, ask=1.09902)
        cand = evaluate(trade, offer, initial_sl_pips=20.0, min_stop_distance_pips=2.0)
        # 初始 SL=1.098 距 bid 恰好 10 pips，远大于 2 pips，不触发 clamp
        self.assertAlmostEqual(cand.new_sl, 1.09800)

        trade2 = make_trade(open_rate=1.10000, is_buy=True, stop_order_id=None)
        offer2 = make_offer(bid=1.09815, ask=1.09817)   # 距初始 SL 仅 1.5 pips < 2 pips
        cand2 = evaluate(trade2, offer2, initial_sl_pips=20.0, min_stop_distance_pips=2.0)
        self.assertAlmostEqual(cand2.new_sl, 1.09795)   # clamp 至 bid - 2 pips


class TestDryRun(unittest.TestCase):
    """real-only 账户的安全阀：dry_run 下绝不发请求。"""

    class FakeFx:
        def __init__(self):
            self.calls = []

        def create_order_request(self, **kwargs):
            self.calls.append(("create", kwargs))
            return "REQUEST"

        def send_request(self, request):
            self.calls.append(("send", request))
            return True

    def test_dry_run_blocks_requests(self):
        from fxcm_api.config import GuardSettings
        from fxcm_api.stop_manager import StopManager

        fake = self.FakeFx()
        mgr = StopManager(fake, GuardSettings(dry_run=True))
        mgr.account_id = "ACC1"
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1",
                           current_stop=1.09800)
        mgr._apply(trade, 1.10010, "BE")
        self.assertEqual(fake.calls, [])

    def test_normal_mode_sends_request(self):
        from fxcm_api.config import GuardSettings
        from fxcm_api.stop_manager import StopManager

        fake = self.FakeFx()
        mgr = StopManager(fake, GuardSettings(dry_run=False))
        mgr.account_id = "ACC1"
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id="S1",
                           current_stop=1.09800)
        mgr._apply(trade, 1.10010, "BE")
        self.assertEqual([c[0] for c in fake.calls], ["create", "send"])

    def test_dry_run_log_dedup(self):
        from fxcm_api.config import GuardSettings
        from fxcm_api.stop_manager import StopManager

        mgr = StopManager(self.FakeFx(), GuardSettings(dry_run=True))
        mgr.account_id = "ACC1"
        trade = make_trade(open_rate=1.10000, is_buy=True, stop_order_id=None)

        with self.assertLogs("fxcm_api.stop_manager", level="INFO") as cm:
            mgr._apply(trade, 1.09800, "INIT")
        self.assertEqual(len(cm.records), 1)

        with self.assertNoLogs("fxcm_api.stop_manager", level="INFO"):
            mgr._apply(trade, 1.09800, "INIT")   # 相同动作不重复刷屏

        with self.assertLogs("fxcm_api.stop_manager", level="INFO") as cm2:
            mgr._apply(trade, 1.09810, "INIT")   # 价位变化时再次输出
        self.assertEqual(len(cm2.records), 1)


class TestDuplicateOrderGuard(unittest.TestCase):
    """ORA-20114 重复单拒绝 = 良性事件；发单后在途窗口内不重复请求。"""

    DUP_MSG = ("Description=19915;DAS 19915: ZDas Exception\n "
               "ORA-20114: Unable to process order 544851263. "
               "Cannot place more than one order of this type within order group.")

    class FakeTable(list):
        @property
        def size(self):
            return len(self)

        def get_row(self, i):
            return self[i]

    class FakeFx:
        def __init__(self, send_exc=None):
            self.send_exc = send_exc
            self.send_count = 0
            accounts = TestDuplicateOrderGuard.FakeTable()
            acc = type("AccRow", (), {"account_id": "ACC1"})()
            accounts.append(acc)
            offer = type("OfferRow", (), {
                "offer_id": "O1", "instrument": "EUR/USD",
                "bid": 1.10000, "ask": 1.10002,
                "point_size": 0.00001, "digits": 5,
            })()
            offers = TestDuplicateOrderGuard.FakeTable([offer])
            trade = type("TradeRow", (), {
                "trade_id": "T1", "offer_id": "O1", "amount": 1000,
                "open_rate": 1.10000, "stop_order_id": "", "stop": 0.0,
                "buy_sell": "B",
            })()
            trades = TestDuplicateOrderGuard.FakeTable([trade])
            self._tables = {
                "accounts": accounts, "offers": offers, "trades": trades,
            }

        def get_table(self, name):
            key = name.name.lower() if hasattr(name, "name") else str(name).lower()
            return self._tables[key]

        def create_order_request(self, **kwargs):
            return "REQUEST"

        def send_request(self, request):
            self.send_count += 1
            if self.send_exc is not None:
                raise self.send_exc
            return True

    def _manager(self, send_exc=None):
        from fxcm_api.config import GuardSettings
        from fxcm_api.stop_manager import StopManager

        fake = self.FakeFx(send_exc=send_exc)
        mgr = StopManager(fake, GuardSettings(dry_run=False))
        return mgr, fake

    def test_duplicate_error_is_benign_and_pends(self):
        mgr, fake = self._manager(send_exc=RuntimeError(self.DUP_MSG))
        with self.assertLogs("fxcm_api.stop_manager", level="INFO") as cm:
            self.assertEqual(mgr.run_cycle(), 0)
        self.assertFalse(any(r.levelname == "ERROR" for r in cm.records))
        self.assertEqual(fake.send_count, 1)
        with self.assertNoLogs("fxcm_api.stop_manager", level="INFO"):
            mgr.run_cycle()   # 在途窗口内：不再发请求
        self.assertEqual(fake.send_count, 1)

    def test_success_sets_pending_window(self):
        mgr, fake = self._manager()
        self.assertEqual(mgr.run_cycle(), 1)
        self.assertEqual(fake.send_count, 1)
        self.assertEqual(mgr.run_cycle(), 0)   # 表格未刷新也不重复下单
        self.assertEqual(fake.send_count, 1)

    def test_other_errors_keep_retrying(self):
        mgr, fake = self._manager(send_exc=RuntimeError("boom"))
        with self.assertLogs("fxcm_api.stop_manager", level="ERROR"):
            mgr.run_cycle()
        mgr.run_cycle()   # 非 DUPLICATE 错误保留原重试语义
        self.assertEqual(fake.send_count, 2)

    def test_classifier_matches_variants(self):
        from fxcm_api.stop_manager import _is_duplicate_stop_error

        self.assertTrue(_is_duplicate_stop_error(RuntimeError(self.DUP_MSG)))
        self.assertTrue(_is_duplicate_stop_error(RuntimeError("ORA-20114: dup")))
        self.assertTrue(
            _is_duplicate_stop_error(RuntimeError("Cannot place more than one "
                                                  "order of this type")))
        self.assertFalse(_is_duplicate_stop_error(RuntimeError("Wait timeout")))
        self.assertFalse(_is_duplicate_stop_error(RuntimeError("boom")))


class TestTradeIsBuy(unittest.TestCase):
    """TRADES 行方向字段是 buy_sell('B'/'S')，没有 is_buy——线上踩过的坑。"""

    class FakeTradeRow:
        def __init__(self, buy_sell):
            self.buy_sell = buy_sell

    def test_buy_sell_b_is_buy(self):
        from fxcm_api.trading import trade_is_buy
        self.assertTrue(trade_is_buy(self.FakeTradeRow("B")))

    def test_buy_sell_s_is_not_buy(self):
        from fxcm_api.trading import trade_is_buy
        self.assertFalse(trade_is_buy(self.FakeTradeRow("S")))


if __name__ == "__main__":
    unittest.main()
