"""optimize 纯函数测试：结果文件命名防碰撞、基准按年归因的报价口径（全离线）。"""
from __future__ import annotations

import unittest

import numpy as np

from quant.optimize import _benchmark_pnl_by_year, result_base_name
from quant.data import OHLCV


class TestResultBaseName(unittest.TestCase):
    def test_label_enters_name_prevents_collision(self):
        a = result_base_name("USD/JPY", "2020-01-01", "2026-09-05", "wfa", "v2_voltp")
        b = result_base_name("USD/JPY", "2020-01-01", "2026-09-05", "wfa", "v2_voltp_p2floor")
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("_wfa_v2_voltp"))
        self.assertEqual(result_base_name("XAU/USD", "s", "e", "sweep", ""),
                         "XAU_USD_s_e_sweep_default")


class TestBenchmarkPnlByYear(unittest.TestCase):
    def test_usd_quote_not_divided_by_close(self):
        # USD 报价（XAU）：两年，价格 100→200 → 10 盎司 MTM 年度 = +1000/年内涨幅×10
        ts = np.array([0, 31_536_000], dtype=np.int64)          # 1970-01-01, 1971-01-01
        ohlcv = OHLCV(ts, np.array([100.0, 100.0]), np.array([100.0, 200.0]),
                      np.array([100.0, 100.0]), np.array([100.0, 200.0]),
                      np.ones(2, dtype=np.int64))
        got = _benchmark_pnl_by_year(ohlcv, 10, "XAU/USD")
        self.assertAlmostEqual(got["1970"], 0.0)                # 首年：年末=入场价
        self.assertAlmostEqual(got["1971"], (200.0 - 100.0) * 10)

    def test_jpy_quote_divides_by_close(self):
        ts = np.array([0, 31_536_000], dtype=np.int64)
        ohlcv = OHLCV(ts, np.array([100.0, 100.0]), np.array([100.0, 200.0]),
                      np.array([100.0, 100.0]), np.array([100.0, 200.0]),
                      np.ones(2, dtype=np.int64))
        got = _benchmark_pnl_by_year(ohlcv, 50_000, "USD/JPY")
        self.assertAlmostEqual(got["1971"], (200.0 - 100.0) * 50_000 / 200.0)


if __name__ == "__main__":
    unittest.main()
