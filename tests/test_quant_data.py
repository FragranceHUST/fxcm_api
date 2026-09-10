"""DataFeed 测试：只读加载、分页完整性与重采样口径。"""
from __future__ import annotations

import tempfile
import unittest

from quant.data import DataFeed
from fxcm_api.data.store import CandleStore


class TestDataFeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CandleStore(f"{self.tmp.name}/candles.db")
        self.feed = DataFeed(f"{self.tmp.name}/candles.db")

    def tearDown(self):
        self.feed.close()
        self.store.close()
        self.tmp.cleanup()

    def test_load_and_resample_1m_to_h4(self):
        # 5 根 1m：前 4 根落在同一 H4 桶，第 5 根进入下一桶
        base = 1_700_000_000 - (1_700_000_000 % 14400)
        bars = [(base + i * 60, 1.0 + i * 0.1, 2.0 + i, 0.5, 1.5 + i * 0.1, i + 1)
                for i in range(4)]
        bars.append((base + 4 * 3600, 3.0, 3.5, 2.9, 3.2, 9))
        self.store.upsert_bid_candles("XAU/USD", 60, bars)
        got = self.feed.load("XAU/USD", 60)
        self.assertEqual(len(got), 5)

        h4 = DataFeed.resample(got, 14400)
        self.assertEqual(len(h4), 2)
        self.assertEqual(int(h4.ts[0]), base - (base % 14400))
        self.assertAlmostEqual(h4.open[0], 1.0)
        self.assertAlmostEqual(h4.high[0], 5.0)     # max(2,3,4,5)
        self.assertAlmostEqual(h4.low[0], 0.5)
        self.assertAlmostEqual(h4.close[0], 1.8)    # 第 4 根 close（1.5+0.1×3）
        self.assertEqual(int(h4.volume[0]), 10)
        self.assertAlmostEqual(h4.open[1], 3.0)

    def test_readonly_connection_rejects_writes(self):
        with self.assertRaises(Exception):
            self.feed._conn.execute(
                "INSERT INTO candles VALUES('X',60,1,1,1,1,1,1,1,1,1,1)")

    def test_empty_range(self):
        got = self.feed.load("NOPE/USD", 60)
        self.assertEqual(len(got), 0)


if __name__ == "__main__":
    unittest.main()
