"""策略绩效单测：compute_strategy_stats / epoch_of / _strategy_trade_ids / 端点结构（全离线）。"""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fxcm_api import daemon as daemon_module
from fxcm_api.config import ArmConfig, StrategySettings
from fxcm_api.data.stats import compute_strategy_stats, epoch_of

def _probe_testclient() -> bool:
    try:
        from fastapi.testclient import TestClient  # noqa: F401
        return True
    except (ImportError, RuntimeError):    # httpx/httpx2 缺失
        return False


def _test_client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


_HAS_TESTCLIENT = _probe_testclient()


def _dt(day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def _closed(tid, is_buy, open_rate, gross_pl, o, c):
    return {"trade_id": tid, "is_buy": is_buy, "open_rate": open_rate,
            "gross_pl": gross_pl, "open_time": o, "close_time": c}


def _flat_keys(v):
    if isinstance(v, dict):
        for k, val in v.items():
            yield k
            yield from _flat_keys(val)
    elif isinstance(v, list):
        for item in v:
            yield from _flat_keys(item)


class TestComputeStrategyStats(unittest.TestCase):
    @staticmethod
    def _window(high, low, calls=None):
        def window(o, c):
            if calls is not None:
                calls.append((o, c))
            return (high, low)
        return window

    def test_long_mfe_mae_asymmetric(self):
        # 多头：mfe = high-open = 0.0080，mae = open-low = 0.0040
        t = {"trade_id": 1, "is_buy": True, "open_rate": 1.1000, "gross_pl": 50.0,
             "open_time": _dt(1, 10), "close_time": _dt(1, 11)}
        s = compute_strategy_stats([t], [], self._window(1.1080, 1.0960))
        self.assertAlmostEqual(s["avg_mfe"], 0.008)
        self.assertAlmostEqual(s["avg_mae"], 0.004)
        self.assertEqual(s["max_mfe"], 0.008)
        self.assertEqual(s["max_mae"], 0.004)
        self.assertEqual(s["excursion_samples"], 1)

    def test_short_direction_semantics(self):
        # 空头：mfe = open-low = 0.0040，mae = high-open = 0.0080（与多头镜像）
        t = {"trade_id": 2, "is_buy": False, "open_rate": 1.1000, "gross_pl": 50.0,
             "open_time": _dt(1, 10), "close_time": _dt(1, 11)}
        s = compute_strategy_stats([t], [], self._window(1.1080, 1.0960))
        self.assertAlmostEqual(s["avg_mfe"], 0.004)
        self.assertAlmostEqual(s["avg_mae"], 0.008)

    def test_negative_excursion_clamped_to_zero(self):
        # 多头亏损笔：high < open → mfe 钳 0
        t = {"trade_id": 3, "is_buy": True, "open_rate": 1.1000, "gross_pl": -50.0,
             "open_time": _dt(1, 10), "close_time": _dt(1, 11)}
        s = compute_strategy_stats([t], [], self._window(1.0980, 1.0900))
        self.assertEqual(s["avg_mfe"], 0.0)
        self.assertEqual(s["max_mfe"], 0.0)
        self.assertAlmostEqual(s["avg_mae"], 0.010)

    def test_window_none_not_counted(self):
        t = {"trade_id": 4, "is_buy": True, "open_rate": 1.1, "gross_pl": 1.0,
             "open_time": _dt(1, 10), "close_time": _dt(1, 11)}
        s = compute_strategy_stats([t], [], lambda o, c: None)
        self.assertEqual(s["excursion_samples"], 0)
        self.assertEqual(s["avg_mfe"], 0.0)
        self.assertEqual(s["avg_mae"], 0.0)
        self.assertEqual(s["max_mfe"], 0.0)
        self.assertEqual(s["max_mae"], 0.0)

    def test_open_trade_uses_now(self):
        calls = []
        t = {"trade_id": 5, "is_buy": True, "open_rate": 1.1000, "gross_pl": 10.0,
             "open_time": _dt(1, 10)}
        now = _dt(1, 12).timestamp()
        s = compute_strategy_stats([], [t], self._window(1.1050, 1.0950, calls), now=now)
        self.assertEqual(calls, [(_dt(1, 10).timestamp(), now)])
        self.assertEqual(s["open_cnt"], 1)
        self.assertAlmostEqual(s["unrealized_pnl"], 10.0)
        self.assertEqual(s["excursion_samples"], 1)

    def test_winrate_pnl_and_holding(self):
        closed = [
            {"trade_id": 1, "is_buy": True, "open_rate": 1.1, "gross_pl": 100.0,
             "open_time": _dt(1, 10), "close_time": _dt(1, 11)},    # 60 min
            {"trade_id": 2, "is_buy": False, "open_rate": 1.1, "gross_pl": -50.0,
             "open_time": _dt(2, 10), "close_time": _dt(2, 12)},    # 120 min
            {"trade_id": 3, "is_buy": True, "open_rate": 1.1, "gross_pl": 20.0,
             "open_time": _dt(3, 10)},                              # 缺 close_time → 不计入
        ]
        s = compute_strategy_stats(closed, [{"gross_pl": 5.5}], lambda o, c: None)
        self.assertEqual(s["closed_cnt"], 3)
        self.assertEqual(s["open_cnt"], 1)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 1)
        self.assertEqual(s["winrate"], round(2 / 3, 4))
        self.assertEqual(s["realized_pnl"], 70.0)
        self.assertEqual(s["unrealized_pnl"], 5.5)
        self.assertEqual(s["total_pnl"], 75.5)
        self.assertEqual(s["avg_holding_minutes"], 90.0)

    def test_empty_inputs(self):
        s = compute_strategy_stats([], [], lambda o, c: (1.0, 0.5))
        self.assertEqual(s["closed_cnt"], 0)
        self.assertEqual(s["winrate"], 0.0)
        self.assertEqual(s["avg_holding_minutes"], 0.0)
        json.dumps(s, allow_nan=False)


class TestEpochOf(unittest.TestCase):
    def test_naive_datetime_is_utc(self):
        # 2026-09-01 10:00 UTC = 1788256800.0；若误用本地时区(Asia/Shanghai)会得 1788228000.0
        self.assertEqual(epoch_of(datetime(2026, 9, 1, 10, 0)), 1788256800.0)

    def test_aware_datetime(self):
        dt = _dt(1, 10)
        self.assertEqual(epoch_of(dt), dt.timestamp())

    def test_numbers_and_none_and_garbage(self):
        self.assertEqual(epoch_of(123), 123.0)
        self.assertEqual(epoch_of(1.5), 1.5)
        self.assertIsNone(epoch_of(None))
        self.assertIsNone(epoch_of("not-a-number"))


class TestStrategyTradeIds(unittest.TestCase):
    def test_mixed_journal(self):
        rows = [
            {"order_type": "quad_entry", "status": "filled",
             "detail": "arm=F1L atr=0.001 trade_id=111"},
            {"order_type": "quad_entry", "detail": "no trade id here"},
            {"order_type": "market", "detail": "trade_id=999"},
            {"order_type": "quad_entry", "status": "unconfirmed", "detail": "trade_id=222"},
            {"order_type": "quad_entry", "detail": ""},
        ]
        self.assertEqual(daemon_module._strategy_trade_ids(rows), {"111", "222"})


class TestStrategyPerformanceEndpoint(unittest.TestCase):
    @staticmethod
    def _settings() -> StrategySettings:
        return StrategySettings(
            enabled=True, symbol="EUR/USD", env="demo", quantity=150000,
            dry_run=True, custom_id_prefix="quad",
            arms=[ArmConfig("F1L", "long", 0.35, 1.3, 10),
                  ArmConfig("F1S", "short", 0.85, 1.3, 8)])

    def _mgr_and_store(self):
        store = MagicMock()
        store.get_journal.return_value = [
            {"order_type": "quad_entry", "status": "filled", "detail": "trade_id=222"},
            {"order_type": "market", "detail": "trade_id=999"},
        ]
        store.get_candles.return_value = []
        worker = MagicMock()
        worker.fx = None                       # 表读取走空兜底；find_offer 异常 → pip=None
        worker.guard.pip_overrides = {}
        mgr = MagicMock()
        mgr.worker.return_value = worker
        return mgr, store

    def _runner(self):
        return SimpleNamespace(
            settings=self._settings(),
            status=lambda: {"enabled": True, "last_eval_ts": 12345,
                            "sl_atr_mult": 1.5, "atr": 0.001,
                            "arms": {"F1L": {"direction": "long", "param1": 0.35,
                                             "trade_id": "222", "holding": True}}})

    def _call_endpoint(self, mgr, store, runner):
        app = daemon_module.build_app(hub=MagicMock(), mgr=mgr, store=store,
                                      daemon_cfg=MagicMock(), tm=MagicMock(),
                                      runner=runner)
        if _HAS_TESTCLIENT:
            return _test_client(app).get("/api/strategy/performance").json()
        from fastapi.routing import APIRoute
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path == "/api/strategy/performance":
                return route.endpoint()
        raise AssertionError("performance 路由未找到")

    @unittest.skipUnless(_HAS_TESTCLIENT, "httpx 缺失，TestClient 冒烟跳过（其余用例仍直接调端点）")
    def test_runner_none_via_testclient(self):
        app = daemon_module.build_app(hub=MagicMock(), mgr=MagicMock(), store=MagicMock(),
                                      daemon_cfg=MagicMock(), tm=MagicMock(), runner=None)
        r = _test_client(app).get("/api/strategy/performance")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"enabled": False})

    def test_runner_none_returns_disabled(self):
        app = daemon_module.build_app(hub=MagicMock(), mgr=MagicMock(), store=MagicMock(),
                                      daemon_cfg=MagicMock(), tm=MagicMock(), runner=None)
        from fastapi.routing import APIRoute
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path == "/api/strategy/performance":
                self.assertEqual(route.endpoint(), {"enabled": False})
                return
        raise AssertionError("performance 路由未找到")

    def test_full_response_structure(self):
        mgr, store = self._mgr_and_store()
        body = self._call_endpoint(mgr, store, self._runner())

        self.assertTrue(body["enabled"])
        self.assertEqual(body["symbol"], "EUR/USD")
        self.assertEqual(body["env"], "demo")
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["quantity"], 150000)
        self.assertEqual(body["lots"], 1.5)
        self.assertEqual(body["last_eval_ts"], 12345)
        self.assertEqual(body["arms"], [
            {"name": "F1L", "direction": "long", "state": "holding", "trade_id": "222"},
            {"name": "F1S", "direction": "short", "state": "waiting", "trade_id": None},
        ])
        stats = body["stats"]
        self.assertEqual(stats["excursion_unit"], "price")
        for key in ("closed_cnt", "open_cnt", "wins", "losses", "winrate", "realized_pnl",
                    "unrealized_pnl", "total_pnl", "avg_holding_minutes", "avg_mfe",
                    "avg_mae", "max_mfe", "max_mae", "excursion_samples"):
            self.assertIn(key, stats)
        # 响应不得泄漏策略参数（runner.status() 原始 dict 含 param1/sl_atr_mult/atr）
        leaked = {"param1", "tp_atr_mult", "sl_atr_mult", "be_pips", "atr", "atr_pips",
                  "band_lower", "band_upper", "prev_close", "cur_close"}
        self.assertEqual(leaked & set(_flat_keys(body)), set())
        json.dumps(body, allow_nan=False)

    def test_in_flight_state(self):
        mgr, store = self._mgr_and_store()
        runner = SimpleNamespace(
            settings=self._settings(),
            status=lambda: {"enabled": True, "arms": {
                "F1S": {"direction": "short", "in_flight": True, "trade_id": None}}})
        body = self._call_endpoint(mgr, store, runner)
        states = {a["name"]: a["state"] for a in body["arms"]}
        self.assertEqual(states, {"F1L": "waiting", "F1S": "in_flight"})


if __name__ == "__main__":
    unittest.main()
