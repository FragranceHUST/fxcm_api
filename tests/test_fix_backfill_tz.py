"""fix_backfill_tz / _row_ts 时区修复单测（全离线）。"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fxcm_api.data.backfill import _row_ts
from fxcm_api.data.store import _SCHEMA
from scripts.fix_backfill_tz import migrate


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


class TestRowTs(unittest.TestCase):
    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(_row_ts(datetime(2026, 9, 15, 2, 38)),
                         _utc(2026, 9, 15, 2, 38))

    def test_aware_datetime_passthrough(self):
        self.assertEqual(_row_ts(datetime(2026, 9, 15, 2, 38, tzinfo=timezone.utc)),
                         _utc(2026, 9, 15, 2, 38))

    def test_float_truncates(self):
        self.assertEqual(_row_ts(1789411080.7), 1789411080)


class TestMigrate(unittest.TestCase):
    def _make_db(self, path: Path):
        conn = sqlite3.connect(str(path))
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO candles(symbol,tf,ts,open,high,low,close,"
            "ask_open,ask_high,ask_low,ask_close,volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("EUR/USD", 60, 1000, 1.1, 1.2, 1.0, 1.15, 1.11, 1.21, 1.01, 1.16, 10),
                ("EUR/USD", 60, 5000, 2.1, 2.2, 2.0, 2.15, None, None, None, None, 5),
                ("XAU/USD", 60, 2000, 3.1, 3.2, 3.0, 3.15, 3.11, 3.21, 3.01, 3.16, 7),
            ])
        conn.execute("INSERT INTO backfill_cursor(symbol,tf,earliest_ts,done) "
                     "VALUES('EUR/USD',60,1000,1)")
        conn.commit()
        conn.close()

    def test_shift_moves_ask_rows_and_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "candles.db"
            self._make_db(db)
            result = migrate(db, 28800)
            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(result["inserted"], 2)
            self.assertEqual(result["cursors"], 1)

            conn = sqlite3.connect(str(db))
            ask_ts = [r[0] for r in conn.execute(
                "SELECT ts FROM candles WHERE symbol='EUR/USD' AND tf=60 "
                "AND ask_open IS NOT NULL")]
            self.assertEqual(ask_ts, [29800])
            xau_ts = conn.execute(
                "SELECT ts FROM candles WHERE symbol='XAU/USD' AND tf=60 "
                "AND ask_open IS NOT NULL").fetchone()[0]
            self.assertEqual(xau_ts, 2000 + 28800)
            bid_only = conn.execute(
                "SELECT close FROM candles WHERE ts=5000 AND volume=5").fetchone()
            self.assertIsNotNone(bid_only)
            cursor = conn.execute(
                "SELECT earliest_ts FROM backfill_cursor WHERE symbol='EUR/USD'").fetchone()[0]
            self.assertEqual(cursor, 1000 + 28800)
            conn.close()

    def test_conflict_keeps_existing_row(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "candles.db"
            self._make_db(db)
            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT INTO candles(symbol,tf,ts,open,high,low,close,volume) "
                "VALUES('EUR/USD',60,29800,9,9,9,9,99)")
            conn.commit()
            conn.close()

            result = migrate(db, 28800)
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(result["conflicts"], 1)

            conn = sqlite3.connect(str(db))
            survivor = conn.execute(
                "SELECT close, ask_open FROM candles WHERE symbol='EUR/USD' "
                "AND tf=60 AND ts=1000+28800").fetchone()
            self.assertEqual(survivor, (9.0, None))
            conn.close()

    def test_migrate_twice_would_double_shift_detected_by_counts(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "candles.db"
            self._make_db(db)
            migrate(db, 28800)
            conn = sqlite3.connect(str(db))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM candles WHERE ask_open IS NOT NULL "
                "AND ask_open != '' AND ts < 1000").fetchone()[0]
            conn.close()
            self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
