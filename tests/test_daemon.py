"""daemon 启动补洞编排 + 交易端点就绪门测试（不登录）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

from fxcm_api import daemon as daemon_module
from fxcm_api.sessions import SessionWorker


def _fake_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.real.fx = object()
    return mgr


def _fx_with_status(name: str | None):
    if name is None:
        return SimpleNamespace(session=None)
    return SimpleNamespace(
        session=SimpleNamespace(session_status=SimpleNamespace(name=name)))


class _BrokenSession:
    """session 属性访问即抛异常的尸体包装器。"""

    @property
    def session(self):
        raise RuntimeError("dead session")


class TestStartupBackfill(unittest.TestCase):
    def test_isolates_per_pair_failures(self):
        calls = []

        def fake_backfill(fx, sym, tf, years, store, delay_ms=300, progress=None,
                          fetch=None, stop_on_known=False):
            calls.append((sym, tf, stop_on_known))
            if sym == "XAU/USD":
                raise RuntimeError("boom")
            return {"bars": 1}

        with patch.object(daemon_module.backfill_module, "backfill", fake_backfill):
            store_mock = MagicMock()
            store_mock.find_gaps.return_value = []
            daemon_module._startup_backfill(mgr=_fake_mgr(), store=store_mock,
                                            symbols=["XAU/USD", "EUR/USD"], days=7.0)

        self.assertEqual(len(calls), 10)  # 5 周期 × 2 品种，单对失败不中断其余
        self.assertEqual({tf for _, tf, _ in calls}, {"1m", "15m", "1h", "4h", "1d"})
        self.assertTrue(all(sk is True for _, _, sk in calls))  # 启动补洞必带 stop_on_known

    def test_disabled_window_skips(self):
        calls = []

        def fake_backfill(fx, sym, tf, years, store, delay_ms=300, progress=None,
                          fetch=None, stop_on_known=False):
            calls.append(1)
            return {"bars": 0}

        with patch.object(daemon_module.backfill_module, "backfill", fake_backfill):
            daemon_module._startup_backfill(mgr=_fake_mgr(), store=MagicMock(),
                                            symbols=["XAU/USD"], days=0.0)

        self.assertEqual(calls, [])


class TestSessionWorkerReadiness(unittest.TestCase):
    """is_ready：仅 CONNECTED 状态可交易（断线窗口 fx 非 None 但 request_factory 为空）。"""

    @staticmethod
    def _worker(fx) -> SessionWorker:
        worker = SessionWorker.__new__(SessionWorker)  # 跳过 config 加载
        worker.fx = fx
        return worker

    def test_none_fx_not_ready(self):
        self.assertFalse(self._worker(None).is_ready())

    def test_connected_ready(self):
        self.assertTrue(self._worker(_fx_with_status("CONNECTED")).is_ready())

    def test_disconnected_and_reconnecting_not_ready(self):
        for name in ("DISCONNECTED", "RECONNECTING", "CONNECTING"):
            self.assertFalse(self._worker(_fx_with_status(name)).is_ready(), name)

    def test_session_none_not_ready(self):
        self.assertFalse(self._worker(_fx_with_status(None)).is_ready())

    def test_broken_session_not_ready(self):
        self.assertFalse(self._worker(_BrokenSession()).is_ready())


class TestTradingGate(unittest.TestCase):
    """交易端点就绪门接线：断线窗口（fx 非 None）必须 503，不得穿透到下单。"""

    @staticmethod
    def _close_endpoint(mgr: MagicMock, store: MagicMock):
        app = daemon_module.build_app(hub=MagicMock(), mgr=mgr, store=store,
                                      daemon_cfg=MagicMock(), tm=MagicMock())
        for route in app.routes:
            if isinstance(route, APIRoute) \
                    and route.path == "/api/{env}/positions/{trade_id}/close" \
                    and "POST" in (route.methods or set()):
                return route.endpoint
        raise AssertionError("close 路由未找到")

    @staticmethod
    def _mgr(ready: bool) -> MagicMock:
        worker = MagicMock()
        worker.fx = object()          # 断线窗口：旧包装器仍非 None
        worker.is_ready.return_value = ready
        worker.status.return_value = {"status": "DISCONNECTED"}
        mgr = MagicMock()
        mgr.worker.return_value = worker
        return mgr

    def test_close_pos_503_when_session_down(self):
        endpoint = self._close_endpoint(self._mgr(ready=False), MagicMock())
        with patch.object(daemon_module, "close_position") as close_mock:
            with self.assertRaises(HTTPException) as ctx:
                endpoint(env="demo", trade_id="123", body=None)
        self.assertEqual(ctx.exception.status_code, 503)
        close_mock.assert_not_called()

    def test_close_pos_proceeds_when_connected(self):
        store = MagicMock()
        mgr = self._mgr(ready=True)
        endpoint = self._close_endpoint(mgr, store)
        with patch.object(daemon_module, "close_position",
                          return_value={"ok": True}) as close_mock:
            result = endpoint(env="demo", trade_id="123", body=None)
        self.assertEqual(result, {"ok": True})
        worker = mgr.worker.return_value
        close_mock.assert_called_once_with(worker.fx, "demo", "123",
                                           amount=None, store=store)


if __name__ == "__main__":
    unittest.main()
