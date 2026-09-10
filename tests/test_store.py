"""store.py / backfill.py 单测：SQLite 往返、游标、分块回填（含容量上限与空窗）。"""

import tempfile
import unittest

from fxcm_api.data.store import CandleStore


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CandleStore(f"{self.tmp.name}/candles.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_bid_roundtrip(self):
        bars = [(i * 60, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, i + 1) for i in range(10)]
        self.assertEqual(self.store.upsert_bid_candles("XAU/USD", 60, bars), 10)
        got = self.store.get_candles("XAU/USD", 60)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[0].ts, 0)
        self.assertEqual((got[0].open, got[0].close, got[0].volume), (1.0, 1.5, 1))

    def test_upsert_idempotent(self):
        bars = [(60, 1.0, 2.0, 0.5, 1.5, 3)]
        self.store.upsert_bid_candles("EUR/USD", 60, bars)
        self.store.upsert_bid_candles("EUR/USD", 60, [(60, 1.0, 2.0, 0.5, 1.6, 4)])
        got = self.store.get_candles("EUR/USD", 60)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].close, 1.6)

    def test_get_candles_range_and_limit(self):
        bars = [(i * 60, 1.0, 1.0, 1.0, 1.0, 1) for i in range(100)]
        self.store.upsert_bid_candles("USD/JPY", 60, bars)
        got = self.store.get_candles("USD/JPY", 60, start_ts=120, end_ts=240)
        self.assertEqual([c.ts for c in got], [120, 180, 240])
        latest3 = self.store.get_candles("USD/JPY", 60, limit=3)
        self.assertEqual([c.ts for c in latest3], [5820, 5880, 5940])

    def test_latest_earliest_count(self):
        bars = [(100, 1.0, 1.0, 1.0, 1.0, 1), (200, 1.0, 1.0, 1.0, 1.0, 1)]
        self.store.upsert_bid_candles("XAU/USD", 900, bars)
        self.assertEqual(self.store.latest_ts("XAU/USD", 900), 200)
        self.assertEqual(self.store.earliest_ts("XAU/USD", 900), 100)
        self.assertEqual(self.store.count("XAU/USD", 900), 2)
        self.assertIsNone(self.store.latest_ts("XAU/USD", 3600))

    def test_full_candles_ask_columns(self):
        self.store.upsert_full_candles("XAU/USD", 3600,
                                       [(0, 1.0, 2.0, 0.5, 1.5,
                                         1.1, 2.1, 0.6, 1.6, 7)])
        self.assertEqual(self.store.count("XAU/USD", 3600), 1)
        got = self.store.get_candles("XAU/USD", 3600)
        self.assertEqual((got[0].open, got[0].close), (1.0, 1.5))

    def test_backfill_cursor(self):
        self.store.save_backfill_cursor("XAU/USD", 60, 12345, False)
        self.assertEqual(self.store.load_backfill_cursor("XAU/USD", 60), (12345, False))
        self.store.save_backfill_cursor("XAU/USD", 60, 99, True)
        self.assertEqual(self.store.load_backfill_cursor("XAU/USD", 60), (99, True))
        self.assertEqual(self.store.load_backfill_cursor("EUR/USD", 60), (None, False))

    def test_journal_and_equity(self):
        jid = self.store.add_journal("demo", "market", symbol="XAU/USD", side="B",
                                     amount=10, requested_rate=4400.1,
                                     filled_rate=4400.25, status="F")
        self.assertGreater(jid, 0)
        rows = self.store.get_journal("demo")
        self.assertEqual(rows[0]["filled_rate"], 4400.25)
        self.store.add_equity_sample("demo", 50000.0, 50120.5, 441.0, ts=1000)
        eq = self.store.get_equity("demo")
        self.assertEqual(eq[0]["equity"], 50120.5)


def make_fake_fetch(start_ts: int, end_ts: int, cap: int):
    """模拟快照分页服务端：区间内每 tf 一根K线、周日无数据、单次最多返回 cap 根（取最新段）。"""
    from datetime import datetime, timezone
    from fxcm_api.data import backfill as bf

    def fetch(_fx, _symbol, tf_name, s, e):
        tf_sec = bf.TF_SECONDS[tf_name]
        lo, hi = max(s, start_ts), min(e, end_ts)
        bars = []
        ts = lo - lo % tf_sec
        while ts <= hi:
            if datetime.fromtimestamp(ts, tz=timezone.utc).weekday() != 6:
                bars.append((ts, 1.0, 1.0, 1.0, 1.0, 1.1, 1.1, 1.1, 1.1, 1))
            ts += tf_sec
        return bars[-cap:]

    return fetch


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CandleStore(f"{self.tmp.name}/candles.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_backfill_walks_with_cap_and_weekend_gaps(self):
        from datetime import datetime, timezone
        from fxcm_api.data import backfill as bf

        now = int(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp())
        span = 3 * 86400  # 3 天
        fetch = make_fake_fetch(now - span, now, cap=300)
        r = bf.backfill(None, "XAU/USD", "1m", span / (365 * 86400), self.store,
                        delay_ms=0, fetch=fetch)
        self.assertEqual(r["bars"], self.store.count("XAU/USD", 60))
        self.assertGreater(r["bars"], 0)
        self.assertIsNotNone(r["earliest"])
        self.assertGreaterEqual(r["earliest"], now - span)
        self.assertTrue(self.store.load_backfill_cursor("XAU/USD", 60)[1])

    def test_backfill_resume_from_latest(self):
        from fxcm_api.data import backfill as bf
        now = 1_800_000_000
        self.store.upsert_bid_candles("EUR/USD", 60, [(now - 60, 1.0, 1.0, 1.0, 1.0, 1)])
        fetch = make_fake_fetch(now - 7200, now, cap=500)
        r = bf.backfill(None, "EUR/USD", "1m", 7200 / (365 * 86400), self.store,
                        delay_ms=0, fetch=fetch)
        self.assertGreaterEqual(r["earliest"], now - 7200)
        bars = self.store.get_candles("EUR/USD", 60, limit=10000)
        ts_list = [b.ts for b in bars]
        self.assertEqual(len(ts_list), len(set(ts_list)))

    def test_backfill_empty_window_terminates(self):
        from fxcm_api.data import backfill as bf
        fetch = make_fake_fetch(9_000_000_000, 9_100_000_000, cap=300)
        r = bf.backfill(None, "XAU/USD", "1m", 1 / 365, self.store, delay_ms=0, fetch=fetch)
        self.assertEqual(r["bars"], 0)
        self.assertTrue(self.store.load_backfill_cursor("XAU/USD", 60)[1])

    def test_backfill_resume_floor_continues_from_earliest(self):
        from fxcm_api.data import backfill as bf
        floor_ts = 1_700_000_000
        self.store.upsert_bid_candles("XAU/USD", 60, [(floor_ts, 1.0, 1.0, 1.0, 1.0, 1)])
        seen = []

        def spy_fetch(_fx, _sym, tf_name, s, e):
            seen.append((s, e))
            return make_fake_fetch(floor_ts - 7200, floor_ts, cap=500)(_fx, _sym, tf_name, s, e)

        r = bf.backfill(None, "XAU/USD", "1m", 3.0, self.store,
                        delay_ms=0, fetch=spy_fetch, resume_floor=True)
        self.assertGreater(r["bars"], 0)
        self.assertEqual(seen[0][1], floor_ts)    # 首个窗口从最早点续挖，而非 latest
        self.assertEqual(r["earliest"], self.store.earliest_ts("XAU/USD", 60))


class TestInsertNewCandles(unittest.TestCase):
    """INSERT OR IGNORE 语义：只统计真正新增，已存在键不覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CandleStore(f"{self.tmp.name}/candles.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_counts_only_genuinely_new(self):
        bar = (1700000000, 1.1, 1.2, 1.0, 1.15, 1.10001, 1.20001, 1.00001, 1.15001, 5)
        self.assertEqual(self.store.insert_new_full_candles("XAU/USD", 60, [bar]), 1)
        self.assertEqual(self.store.insert_new_full_candles("XAU/USD", 60, [bar]), 0)
        self.assertEqual(self.store.insert_new_full_candles(
            "XAU/USD", 60, [(bar[0] + 60,) + bar[1:]]), 1)
        rows = self.store.get_candles("XAU/USD", 60, start_ts=1700000000, end_ts=1700000000)
        self.assertEqual(rows[0].volume, 5)     # IGNORE 不覆盖已存在行


class TestStopOnKnown(unittest.TestCase):
    """启动补洞：撞到整块已知数据即停，不重扫整个回看窗口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CandleStore(f"{self.tmp.name}/candles.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _bar(ts):
        return (ts, 1.0, 1.2, 0.9, 1.1, 1.10001, 1.20001, 0.90001, 1.10002, 1)

    @staticmethod
    def _minute_fetch(log):
        def fetch(_fx, _symbol, _tf, start, end):
            log.append((start, end))
            first = (start // 60) * 60
            return [TestStopOnKnown._bar(ts) for ts in range(first, end, 60)]
        return fetch

    def test_no_downtime_single_request(self):
        """无停机重启：首个请求整块已知 → 立即停（1 请求 0 新增）。"""
        import time
        from fxcm_api.data import backfill as bf
        shift = (int(time.time()) // 60) * 60 - 4500   # 对齐整分（与真实 K 线网格一致）
        self.store.upsert_full_candles(
            "XAU/USD", 60, [self._bar(shift + ts) for ts in range(0, 4500, 60)])
        log = []
        r = bf.backfill(None, "XAU/USD", "1m", 4500 / (365 * 86400), self.store,
                        delay_ms=0, fetch=self._minute_fetch(log), stop_on_known=True)
        self.assertEqual(r["bars"], 0)
        self.assertEqual(len(log), 1)

    def test_gap_filled_then_stop(self):
        """停机缺口：只补缺口区（2700..4380 共 29 根），触到存量即停。"""
        import time
        from fxcm_api.data import backfill as bf
        shift = (int(time.time()) // 60) * 60 - 4500   # 对齐整分（与真实 K 线网格一致）
        # 存量：0..2640（旧）+ 4440（重启后 live 写入），中间 2700..4380 为停机缺口
        self.store.upsert_full_candles(
            "XAU/USD", 60, [self._bar(shift + ts) for ts in range(0, 2641, 60)])
        self.store.upsert_full_candles("XAU/USD", 60, [self._bar(shift + 4440)])
        log = []
        r = bf.backfill(None, "XAU/USD", "1m", 4500 / (365 * 86400), self.store,
                        delay_ms=0, fetch=self._minute_fetch(log), stop_on_known=True)
        self.assertEqual(r["bars"], 29)   # 只补 2700..4380 的缺口，存量 0..2640 与 4440 不重写
        self.assertEqual(len(log), 1)     # 1m 分块 CHUNK=4440s ≥ 测试窗口，一请求即覆盖


if __name__ == "__main__":
    unittest.main()
