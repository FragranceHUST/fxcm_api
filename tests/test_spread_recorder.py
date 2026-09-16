"""SpreadRecorder 纯逻辑测试：pip 换算、CSV 落盘、日切、过期 tick 过滤（不登录 FXCM）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fxcm_api.spread_recorder import SpreadRecorder, default_pip_size, spread_row


def _fixed_provider(quotes: dict[str, tuple[float, float]], tick_ts: float):
    return lambda: {sym: (bid, ask, tick_ts) for sym, (bid, ask) in quotes.items()}


class TestPip(unittest.TestCase):
    def test_default_pip_size(self) -> None:
        self.assertEqual(default_pip_size("EUR/USD"), 0.0001)
        self.assertEqual(default_pip_size("USD/JPY"), 0.01)
        self.assertEqual(default_pip_size("GBP/JPY"), 0.01)

    def test_overrides_win(self) -> None:
        rec = SpreadRecorder(lambda: {}, Path("/tmp"), pip_overrides={"XAU/USD": 0.1})
        self.assertEqual(rec.pip_for("XAU/USD"), 0.1)
        self.assertEqual(rec.pip_for("EUR/USD"), 0.0001)

    def test_spread_row_math(self) -> None:
        row = spread_row(100.0, "EUR/USD", 1.08321, 1.08333, 0.0001)
        self.assertAlmostEqual(float(row["spread_price"]), 0.00012)
        self.assertAlmostEqual(float(row["spread_pips"]), 1.2)
        row_xau = spread_row(100.0, "XAU/USD", 2400.50, 2400.85, 0.1)
        self.assertAlmostEqual(float(row_xau["spread_pips"]), 3.5)


class TestRecorder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rec(self, quotes, tick_ts, clock, overrides=None) -> SpreadRecorder:
        return SpreadRecorder(_fixed_provider(quotes, tick_ts), Path(self.tmp.name),
                              interval_sec=5.0, pip_overrides=overrides, clock=clock)

    def test_sample_once_writes_header_and_appends(self) -> None:
        rec = self._rec({"EUR/USD": (1.08321, 1.08333)}, 1000.0, lambda: 1000.0)
        rows = rec.sample_once()
        self.assertEqual(len(rows), 1)
        path = Path(self.tmp.name) / "spread_19700101.csv"
        lines = path.read_text().strip().splitlines()
        self.assertEqual(lines[0], "ts,symbol,bid,ask,spread_price,spread_pips")
        self.assertEqual(len(lines), 2)
        rec.sample_once()                      # 重复采样：追加行，不重复写头
        self.assertEqual(len(path.read_text().strip().splitlines()), 3)
        rec.close()

    def test_multi_symbol_rows_ordered_with_pips(self) -> None:
        rec = self._rec({"EUR/USD": (1.08321, 1.08333),
                         "USD/JPY": (150.123, 150.133),
                         "XAU/USD": (2400.50, 2400.85)}, 1000.0, lambda: 1000.0,
                        overrides={"XAU/USD": 0.1})
        rows = rec.sample_once()
        by_sym = {r["symbol"]: r for r in rows}
        self.assertAlmostEqual(float(by_sym["USD/JPY"]["spread_pips"]), 1.0)
        self.assertAlmostEqual(float(by_sym["XAU/USD"]["spread_pips"]), 3.5)
        rec.close()

    def test_date_rollover_creates_second_file(self) -> None:
        times = [0.0, 86400.0]                 # UTC day1 00:00 → day2 00:00

        def provider():
            ts = times[0] if times else 9e9
            return {"EUR/USD": (1.0, 1.0, ts)}

        rec = SpreadRecorder(provider, Path(self.tmp.name),
                             clock=lambda: times.pop(0) if times else 9e9)
        rec.sample_once()
        rec.sample_once()
        d1 = Path(self.tmp.name) / "spread_19700101.csv"
        d2 = Path(self.tmp.name) / "spread_19700102.csv"
        self.assertTrue(d1.exists() and d2.exists())
        self.assertEqual(len(d1.read_text().strip().splitlines()), 2)
        self.assertEqual(len(d2.read_text().strip().splitlines()), 2)
        rec.close()

    def test_stale_tick_skipped(self) -> None:
        rec = self._rec({"EUR/USD": (1.0, 1.0)}, 1000.0 - 121, lambda: 1000.0)
        self.assertEqual(rec.sample_once(), [])
        rec.close()

    def test_fresh_tick_age_edge_kept(self) -> None:
        rec = self._rec({"EUR/USD": (1.0, 1.0)}, 1000.0 - 120, lambda: 1000.0)
        self.assertEqual(len(rec.sample_once()), 1)   # 恰好 120s 不算过期
        rec.close()


if __name__ == "__main__":
    unittest.main()
