"""行情面自愈：fx_market_open 时段判定、staleness 保鲜判定、hub 真换绑回归（不登录）。"""

import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from forexconnect import TableListener

from fxcm_api import daemon as daemon_module
from fxcm_api.data.hub import MarketHub
from fxcm_api.sessions import SessionWorker, fx_market_open


def _et_ts(y: int, m: int, d: int, hh: int, mm: int) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).timestamp()


class TestFxMarketOpen(unittest.TestCase):
    # 2026-09: 16=周三 18=周五 19=周六 20=周日
    def test_weekday_midday_open(self):
        self.assertTrue(fx_market_open(_et_ts(2026, 9, 16, 12, 0)))

    def test_friday_close_boundary(self):
        self.assertTrue(fx_market_open(_et_ts(2026, 9, 18, 16, 50)))   # 收盘前、维护窗外
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 18, 16, 59)))  # 已入维护窗口
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 18, 17, 1)))

    def test_weekend_closed(self):
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 19, 12, 0)))
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 20, 16, 59)))

    def test_sunday_reopen_after_maintenance(self):
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 20, 17, 5)))   # 滚动维护窗口
        self.assertTrue(fx_market_open(_et_ts(2026, 9, 20, 17, 11)))

    def test_daily_maintenance_window(self):
        self.assertFalse(fx_market_open(_et_ts(2026, 9, 16, 17, 5)))
        self.assertTrue(fx_market_open(_et_ts(2026, 9, 16, 17, 11)))

    def test_winter_dst_independent(self):
        self.assertTrue(fx_market_open(_et_ts(2027, 1, 13, 12, 0)))    # 2027-01-13 周三（EST）


class _StubHub(MarketHub):
    """绕过真实 __init__ 的最小行情桩（tick 数据由测试注入）。"""

    def __init__(self, ticks: dict):
        self.symbols = list(ticks)
        self._ticks = ticks

    def last_tick(self, symbol: str):
        return self._ticks.get(symbol)


class TestStalenessCheck(unittest.TestCase):
    def _check(self, ticks: dict, threshold: float = 120.0):
        return daemon_module._make_staleness_check(_StubHub(ticks), threshold)

    def test_threshold_zero_disables(self):
        check = self._check({"EUR/USD": None}, threshold=0.0)
        self.assertFalse(check())

    def test_market_closed_never_stale(self):
        check = self._check({"EUR/USD": (1.0, 1.1, 0.0)})
        with patch.object(daemon_module, "fx_market_open", lambda: False):
            self.assertFalse(check())

    def test_any_fresh_tick_keeps_alive(self):
        now = time.time()
        check = self._check({"EUR/USD": (1.0, 1.1, now - 300.0),   # 停更
                             "XAU/USD": (1.0, 1.1, now - 5.0)})    # 新鲜
        with patch.object(daemon_module, "fx_market_open", lambda: True):
            self.assertFalse(check())

    def test_all_stale_triggers(self):
        now = time.time()
        check = self._check({"EUR/USD": (1.0, 1.1, now - 300.0),
                             "XAU/USD": (1.0, 1.1, now - 300.0)})
        with patch.object(daemon_module, "fx_market_open", lambda: True):
            self.assertTrue(check())

    def test_never_ticked_grace_period(self):
        check = self._check({"EUR/USD": None})
        real_time = time.time              # 先绑定真函数，patch 后 lambda 内不再递归
        with patch.object(daemon_module, "fx_market_open", lambda: True), \
             patch.object(daemon_module.time, "time",
                          side_effect=lambda: real_time() + 30.0):
            self.assertFalse(check())                              # 启动宽限期内
        with patch.object(daemon_module, "fx_market_open", lambda: True), \
             patch.object(daemon_module.time, "time",
                          side_effect=lambda: real_time() + 300.0):
            self.assertTrue(check())                               # 宽限期已过


class _FakeOfferRow:
    def __init__(self, offer_id: str, instrument: str, bid: float = 1.1, ask: float = 1.2):
        self.offer_id = offer_id
        self.instrument = instrument
        self.bid = bid
        self.ask = ask


class _FakeTable:
    def __init__(self, rows: list[_FakeOfferRow]):
        self.rows = rows
        self.subscribed: list[tuple[int, TableListener]] = []
        self.unsubscribed: list[tuple[int, TableListener]] = []

    def __iter__(self):
        return iter(self.rows)

    def subscribe_update(self, utype, listener):
        self.subscribed.append((utype, listener))

    def unsubscribe_update(self, utype, listener):
        self.unsubscribed.append((utype, listener))


class _FakeFx:
    def __init__(self, rows: list[_FakeOfferRow]):
        self._table = _FakeTable(rows)

    def get_table(self, _table_type):
        return self._table


class TestHubResubscribe(unittest.TestCase):
    """回归：包装器 subscribe() 二次调用会静默忽略新表，必须换新 listener。"""

    def test_resubscribe_binds_new_session_table(self):
        fx1 = _FakeFx([_FakeOfferRow("1", "EUR/USD")])
        hub = MarketHub(fx1, ["EUR/USD"], MagicMock())
        self.assertEqual(len(fx1._table.subscribed), 1)

        fx2 = _FakeFx([_FakeOfferRow("9", "EUR/USD")])
        hub.resubscribe(fx2)

        self.assertEqual(len(fx2._table.subscribed), 1)   # 新表已订阅（旧实现为 0）
        self.assertEqual(len(fx1._table.subscribed), 1)   # 旧表未追加（旧实现会重复订阅）

    def test_ticks_flow_after_resubscribe(self):
        fx1 = _FakeFx([_FakeOfferRow("1", "EUR/USD")])
        hub = MarketHub(fx1, ["EUR/USD"], MagicMock())

        fx2 = _FakeFx([_FakeOfferRow("9", "EUR/USD")])
        hub.resubscribe(fx2)

        listener = fx2._table.subscribed[0][1]
        listener.on_changed("r1", _FakeOfferRow("9", "EUR/USD", bid=1.15, ask=1.16))
        tick = hub.last_tick("EUR/USD")
        assert tick is not None
        self.assertEqual((tick[0], tick[1]), (1.15, 1.16))


class TestStaleReloadDecision(unittest.TestCase):
    def _worker(self, check, last_reload: float) -> SessionWorker:
        w = SessionWorker.__new__(SessionWorker)
        w.env = "real"
        w.staleness_check = check
        w._last_stale_reload = last_reload
        return w

    def test_no_check_no_reload(self):
        self.assertFalse(self._worker(None, 0.0)._stale_reload_due())

    def test_stale_and_elapsed_triggers(self):
        self.assertTrue(self._worker(lambda: True, 0.0)._stale_reload_due())

    def test_min_interval_throttles(self):
        w = self._worker(lambda: True, time.time())
        self.assertFalse(w._stale_reload_due())

    def test_check_exception_swallowed(self):
        def boom():
            raise RuntimeError("hub dead")
        self.assertFalse(self._worker(boom, 0.0)._stale_reload_due())


if __name__ == "__main__":
    unittest.main()
