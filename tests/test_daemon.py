"""daemon 启动补洞编排测试（不登录）。"""

import unittest
from unittest.mock import MagicMock, patch

from fxcm_api import daemon as daemon_module


def _fake_mgr() -> MagicMock:
    mgr = MagicMock()
    mgr.real.fx = object()
    return mgr


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


if __name__ == "__main__":
    unittest.main()
