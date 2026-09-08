"""candles.py / csv_store.py 单测：桶数学、多周期推进、rebuild、线程安全、CSV 往返。"""

import tempfile
import threading
import unittest

from fxcm_api.candles import Candle, CandleAggregator, bucket_start, rebuild
from fxcm_api.csv_store import CsvTickStore, csv_path_for


class TestBucket(unittest.TestCase):
    def test_bucket_start_boundaries(self):
        self.assertEqual(bucket_start(0.0, 1), 0)
        self.assertEqual(bucket_start(0.9, 1), 0)
        self.assertEqual(bucket_start(1.0, 1), 1)
        self.assertEqual(bucket_start(59.9, 60), 0)
        self.assertEqual(bucket_start(60.0, 60), 60)
        self.assertEqual(bucket_start(900.0, 900), 900)
        self.assertEqual(bucket_start(3599.9, 3600), 0)


class TestAggregator(unittest.TestCase):
    def test_single_timeframe_ohlc(self):
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        # 同一秒内 3 个 tick：open=首tick, high/low 取极值, close=末tick
        agg.on_tick(0.1, bid=100.0, ask=100.2)
        agg.on_tick(0.5, bid=101.0, ask=101.2)
        agg.on_tick(0.8, bid=99.5, ask=99.7)
        bars = agg.candles(1)
        self.assertEqual(len(bars), 1)
        c = bars[0]
        self.assertEqual((c.open, c.high, c.low, c.close, c.volume), (100.0, 101.0, 99.5, 99.5, 3))

    def test_second_rollover(self):
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        agg.on_tick(0.1, 100.0, 100.2)
        agg.on_tick(1.1, 105.0, 105.2)  # 下一秒 → 新桶
        bars = agg.candles(1)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ts, 0)
        self.assertEqual(bars[1].ts, 1)
        self.assertEqual(bars[1].close, 105.0)

    def test_multi_timeframe_from_ticks(self):
        # 121 个 tick（每秒一个，bid=i），覆盖 1m 桶滚动
        agg = CandleAggregator("EUR/USD", granularities=(1, 60))
        for i in range(121):
            agg.on_tick(float(i) + 0.5, bid=float(i), ask=float(i) + 0.01)
        self.assertEqual(len(agg.candles(1)), 121)
        m1 = agg.candles(60)
        self.assertEqual(len(m1), 3)  # 桶0(0-59)、桶60(60-119)、桶120(1个tick)
        self.assertEqual(m1[0].ts, 0)
        self.assertEqual((m1[0].open, m1[0].close, m1[0].volume), (0.0, 59.0, 60))
        self.assertEqual(m1[1].ts, 60)
        self.assertEqual(m1[2].ts, 120)
        self.assertEqual(m1[2].volume, 1)

    def test_limit_returns_latest(self):
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        for i in range(10):
            agg.on_tick(float(i), bid=float(i), ask=float(i))
        bars = agg.candles(1, limit=3)
        self.assertEqual([b.ts for b in bars], [7, 8, 9])

    def test_rebuild_1s_to_1m(self):
        candles_1s = [Candle(ts=i, open=float(i), high=float(i) + 0.5,
                             low=float(i) - 0.5, close=float(i) + 0.2, volume=3)
                      for i in range(120)]
        m1 = rebuild(candles_1s, 60)
        self.assertEqual(len(m1), 2)
        self.assertEqual((m1[0].ts, m1[0].open, m1[0].high, m1[0].low, m1[0].close),
                         (0, 0.0, 59.5, -0.5, 59.2))
        self.assertEqual(m1[0].volume, 180)

    def test_load_history_rebuilds_higher_tf(self):
        agg = CandleAggregator("XAU/USD", granularities=(1, 60))
        candles_1s = [Candle(ts=i, open=1.0, high=1.0, low=1.0, close=1.0, volume=2)
                      for i in range(120)]
        agg.load_history(candles_1s)
        self.assertEqual(len(agg.candles(1)), 120)
        self.assertEqual(len(agg.candles(60)), 2)
        # 历史加载后继续吃 tick：新 tick 接到桶 120
        agg.on_tick(120.5, bid=2.0, ask=2.01)
        self.assertEqual(len(agg.candles(60)), 3)

    def test_load_history_seeds_last_tick(self):
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        agg.load_history([Candle(ts=100, open=10.0, high=11.0, low=9.5, close=10.8, volume=7)])
        self.assertEqual(agg.last_tick(), (10.8, 10.8, 100.0))
        agg.on_tick(101.0, bid=11.0, ask=11.2)
        self.assertEqual(agg.last_tick(), (11.0, 11.2, 101.0))

    def test_thread_safety_smoke(self):
        # 聚合器契约：ts 单调递增（fxcorepy 单线程回调保证）。
        # 并发正确性用"多线程抢同一桶"验证：锁串行化后 volume 必须精确累加。
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        n_threads, n_ticks = 8, 500
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for i in range(n_ticks):
                agg.on_tick(0.5 + i * 1e-6, bid=1.0, ask=1.01)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(agg.stats()["tick_count"], n_threads * n_ticks)
        bars = agg.candles(1)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].volume, n_threads * n_ticks)

    def test_last_tick_snapshot(self):
        agg = CandleAggregator("XAU/USD", granularities=(1,))
        self.assertIsNone(agg.last_tick())
        agg.on_tick(5.0, bid=10.0, ask=10.2)
        self.assertEqual(agg.last_tick(), (10.0, 10.2, 5.0))


class TestCsvStore(unittest.TestCase):
    def test_csv_path_for(self):
        self.assertEqual(csv_path_for("data", "XAU/USD").as_posix(), "data/XAU_USD_1s.csv")
        self.assertEqual(csv_path_for("data", "EUR/USD").as_posix(), "data/EUR_USD_1s.csv")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CsvTickStore(tmp, "XAU/USD")
            self.assertEqual(store.load(), [])  # 空目录
            store.open_for_append()
            bars = [Candle(ts=i, open=1.0, high=2.0, low=0.5, close=1.5, volume=i + 1)
                    for i in range(5)]
            for b in bars:
                store.append(b)
            store.close()
            loaded = CsvTickStore(tmp, "XAU/USD").load()
            self.assertEqual(loaded, bars)

    def test_header_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CsvTickStore(tmp, "EUR/USD")
            store.open_for_append()
            store.close()
            store2 = CsvTickStore(tmp, "EUR/USD")  # 第二次打开（追加模式）
            store2.open_for_append()
            store2.append(Candle(ts=99, open=1, high=1, low=1, close=1, volume=1))
            store2.close()
            lines = (store2.path).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines[0], "ts,open,high,low,close,volume")
            self.assertEqual(len(lines), 2)  # 表头 + 1 行数据，无重复表头

    def test_corrupt_rows_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = csv_path_for(tmp, "USD/JPY")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "ts,open,high,low,close,volume\n"
                "0,1,2,0.5,1.5,3\n"
                "bad,row,here\n"
                "1,1,2,0.5,1.5,2\n",
                encoding="utf-8",
            )
            loaded = CsvTickStore(tmp, "USD/JPY").load()
            self.assertEqual([c.ts for c in loaded], [0, 1])


if __name__ == "__main__":
    unittest.main()
