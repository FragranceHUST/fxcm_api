"""backfill_official 单测：DateTime/年份/品种映射/FullBar 转换/幂等导入/基差对比（全离线）。"""

import tempfile
import unittest
from datetime import datetime, timezone

from fxcm_api.data.store import CandleStore
from scripts.backfill_official import (
    CSV_HEADER,
    compare_official_vs_main,
    map_symbol,
    parse_datetime_utc,
    parse_week_csv,
    parse_years,
    run_backfill,
)

SAMPLE_ROW_1 = "01/04/2026 22:07:00.000,1.17204,1.17210,1.17200,1.17206,1.17210,1.17216,1.17206,1.17212"
SAMPLE_ROW_2 = "01/04/2026 22:08:00.000,1.17206,1.17212,1.17204,1.17208,1.17212,1.17218,1.17210,1.17214"


def _week_csv(*rows: str) -> str:
    return CSV_HEADER + "\n" + "\n".join(rows) + "\n"


def _utc(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


class TestParseDateTime(unittest.TestCase):
    def test_with_millis_utc(self):
        self.assertEqual(parse_datetime_utc("01/04/2026 22:07:00.000"),
                         _utc(2026, 1, 4, 22, 7))

    def test_minute_alignment(self):
        self.assertEqual(parse_datetime_utc("01/04/2026 22:07:00.000") % 60, 0)

    def test_sub_minute_rejected(self):
        with self.assertRaises(ValueError):
            parse_datetime_utc("01/04/2026 22:07:30.500")

    def test_invalid_format_rejected(self):
        with self.assertRaises(ValueError):
            parse_datetime_utc("2026-01-04 22:07:00")
        with self.assertRaises(ValueError):
            parse_datetime_utc("not-a-date")


class TestParseYears(unittest.TestCase):
    def test_single(self):
        self.assertEqual(parse_years("2020"), [2020])

    def test_range(self):
        self.assertEqual(parse_years("2017-2019"), [2017, 2018, 2019])

    def test_mixed(self):
        self.assertEqual(parse_years("2021,2023,2025-2026"), [2021, 2023, 2025, 2026])

    def test_dedup_and_sort(self):
        self.assertEqual(parse_years("2024,2022-2023,2024"), [2022, 2023, 2024])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_years("2025-2020")
        with self.assertRaises(ValueError):
            parse_years("abc")
        with self.assertRaises(ValueError):
            parse_years("")


class TestMapSymbol(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(map_symbol("EURUSD"), "EUR/USD")
        self.assertEqual(map_symbol("USDJPY"), "USD/JPY")
        self.assertEqual(map_symbol("XAUUSD"), "XAU/USD")

    def test_case_and_slash_passthrough(self):
        self.assertEqual(map_symbol("eurusd"), "EUR/USD")
        self.assertEqual(map_symbol("EUR/USD"), "EUR/USD")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            map_symbol("EUR")
        with self.assertRaises(ValueError):
            map_symbol("EURUSDX")
        with self.assertRaises(ValueError):
            map_symbol("EUR1SD")


class TestParseWeekCsv(unittest.TestCase):
    def test_rows_to_fullbars(self):
        bars = parse_week_csv(_week_csv(SAMPLE_ROW_1, SAMPLE_ROW_2))
        self.assertEqual(len(bars), 2)
        ts1 = _utc(2026, 1, 4, 22, 7)
        self.assertEqual(
            bars[0],
            (ts1, 1.17204, 1.17210, 1.17200, 1.17206,
             1.17210, 1.17216, 1.17206, 1.17212, 0))
        self.assertEqual(bars[1][0], ts1 + 60)

    def test_blank_lines_skipped(self):
        bars = parse_week_csv(CSV_HEADER + "\n\n" + SAMPLE_ROW_1 + "\n\n")
        self.assertEqual(len(bars), 1)

    def test_bad_header(self):
        with self.assertRaises(ValueError):
            parse_week_csv("Time,Open\n" + SAMPLE_ROW_1)

    def test_bad_row_rejected(self):
        bad = "01/04/2026 22:07:00.000,1.17,x,1.17,1.17,1.17,1.17,1.17,1.17"
        with self.assertRaises(ValueError):
            parse_week_csv(_week_csv(bad))


class TestIdempotentImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = f"{self.tmp.name}/candles_official.db"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _fake_fetch(hit_weeks=(1,)):
        def fetch(instrument, year, week):
            if week in hit_weeks:
                return _week_csv(SAMPLE_ROW_1, SAMPLE_ROW_2)
            return None
        return fetch

    def test_first_and_second_run(self):
        stats = run_backfill(["EURUSD"], [2026], self.db, delay_ms=0,
                             fetch=self._fake_fetch(), log=lambda *_: None)
        r = stats[0]
        self.assertEqual((r["symbol"], r["imported"], r["rows"]), ("EUR/USD", 2, 2))
        self.assertEqual((r["weeks_ok"], r["weeks_404"], r["weeks_failed"]), (1, 52, 0))
        self.assertEqual(r["days"], 1)
        store = CandleStore(self.db)
        try:
            self.assertEqual(store.count("EUR/USD", 60), 2)
        finally:
            store.close()

        stats2 = run_backfill(["EURUSD"], [2026], self.db, delay_ms=0,
                              fetch=self._fake_fetch(), log=lambda *_: None)
        self.assertEqual(stats2[0]["imported"], 0)
        store = CandleStore(self.db)
        try:
            self.assertEqual(store.count("EUR/USD", 60), 2)
        finally:
            store.close()

    def test_failed_week_counted(self):
        def fetch(instrument, year, week):
            if week == 1:
                raise OSError("network down")
            return None

        stats = run_backfill(["USDJPY"], [2025], self.db, delay_ms=0,
                             fetch=fetch, log=lambda *_: None)
        self.assertEqual((stats[0]["weeks_failed"], stats[0]["weeks_404"]), (1, 52))
        self.assertEqual(stats[0]["imported"], 0)


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main_db = f"{self.tmp.name}/main.db"
        self.official_db = f"{self.tmp.name}/official.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_overlap_diff_and_coverage(self):
        main = CandleStore(self.main_db)
        off = CandleStore(self.official_db)
        try:
            main.upsert_full_candles("EUR/USD", 60, [
                (0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1),
                (60, 1.0, 1.0, 1.0, 1.1, 1.0, 1.0, 1.0, 1.1, 1),
                (120, 1.0, 1.0, 1.0, 1.2, 1.0, 1.0, 1.0, 1.2, 1)])
            off.insert_new_full_candles("EUR/USD", 60, [
                (0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0),
                (60, 1.0, 1.0, 1.0, 1.2, 1.0, 1.0, 1.0, 1.2, 0)])
        finally:
            main.close()
            off.close()
        r = compare_official_vs_main(self.main_db, self.official_db, ["EUR/USD"])[0]
        self.assertEqual(r["overlap"], 2)
        self.assertEqual(r["days"], 1)
        self.assertEqual(r["main_in_range"], 2)
        self.assertAlmostEqual(r["coverage"], 1.0)
        self.assertAlmostEqual(r["median"], 0.05)
        self.assertAlmostEqual(r["p90"], 0.1)
        self.assertAlmostEqual(r["max_abs"], 0.1)

    def test_zero_overlap(self):
        main = CandleStore(self.main_db)
        off = CandleStore(self.official_db)
        try:
            main.upsert_bid_candles("EUR/USD", 60, [(0, 1.0, 1.0, 1.0, 1.0, 1)])
            off.insert_new_full_candles("EUR/USD", 60,
                                        [(999999, 1.0, 1.0, 1.0, 1.0,
                                          1.0, 1.0, 1.0, 1.0, 0)])
        finally:
            main.close()
            off.close()
        r = compare_official_vs_main(self.main_db, self.official_db, ["EUR/USD"])[0]
        self.assertEqual(r["overlap"], 0)
        self.assertIsNone(r["median"])
        self.assertIsNone(r["coverage"])


if __name__ == "__main__":
    unittest.main()


class TestResolveDb(unittest.TestCase):
    """quant.cli.resolve_db 数据源路由：显式优先 → 官方库有品种即用 → 回退主库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.official = f"{self._tmp.name}/official.db"
        self.main = f"{self._tmp.name}/main.db"
        for path in (self.official, self.main):
            conn = CandleStore(path)  # __init__ 建 schema
            conn.close()
        conn = CandleStore(self.official)
        conn.insert_new_full_candles("EUR/USD", 60, [(_utc(2026, 1, 5, 22, 7), 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0)])
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_db_wins(self):
        from quant.cli import resolve_db
        path, src = resolve_db("EUR/USD", "custom.db", self.official, self.main)
        self.assertEqual(path, "custom.db")
        self.assertEqual(src, "显式指定")

    def test_official_hit(self):
        from quant.cli import resolve_db
        path, src = resolve_db("EUR/USD", None, self.official, self.main)
        self.assertEqual(path, self.official)
        self.assertEqual(src, "官方数据源")

    def test_official_miss_falls_back_to_main(self):
        from quant.cli import resolve_db
        path, src = resolve_db("XAU/USD", None, self.official, self.main)
        self.assertEqual(path, self.main)
        self.assertEqual(src, "主库")

    def test_official_missing_file_falls_back(self):
        from quant.cli import resolve_db
        path, src = resolve_db("EUR/USD", None, f"{self._tmp.name}/nope.db", self.main)
        self.assertEqual(path, self.main)
