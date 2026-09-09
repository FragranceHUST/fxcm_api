"""SQLite 存储层：K线（m1/m15/H1/H4/D1）/ 净值采样 / 订单日志 / 回填游标。

get_candles() 是量化回测与实盘系统的统一数据读取入口（bid 口径 OHLC + tick volume；
ask 四价在库中保留，可用 SQL 直查）。线程安全：单连接 + 全局锁（写入为低频批量）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from fxcm_api.candles import Candle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT NOT NULL, tf INTEGER NOT NULL, ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
  volume INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(symbol, tf, ts));
CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_ts ON candles(symbol, tf, ts);
CREATE TABLE IF NOT EXISTS equity_samples(
  env TEXT NOT NULL, ts INTEGER NOT NULL,
  balance REAL, equity REAL, margin_used REAL,
  PRIMARY KEY(env, ts));
CREATE TABLE IF NOT EXISTS order_journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env TEXT NOT NULL, ts INTEGER NOT NULL, order_type TEXT NOT NULL,
  symbol TEXT, side TEXT, amount INTEGER,
  requested_rate REAL, filled_rate REAL, status TEXT, sl REAL, tp REAL,
  detail TEXT);
CREATE TABLE IF NOT EXISTS backfill_cursor(
  symbol TEXT NOT NULL, tf INTEGER NOT NULL,
  earliest_ts INTEGER, done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(symbol, tf));
"""

# (ts, bid o/h/l/c, volume)
BidBar = tuple[int, float, float, float, float, int]
# (ts, bid o/h/l/c, ask o/h/l/c, volume)
FullBar = tuple[int, float, float, float, float, float, float, float, float, int]


class CandleStore:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- K线 ----

    def upsert_bid_candles(self, symbol: str, tf: int, bars: list[BidBar]) -> int:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol,tf,ts,open,high,low,close,volume) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(symbol, tf, b[0], b[1], b[2], b[3], b[4], b[5]) for b in bars])
            self._conn.commit()
            return len(bars)

    def upsert_full_candles(self, symbol: str, tf: int, bars: list[FullBar]) -> int:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol,tf,ts,open,high,low,close,"
                "ask_open,ask_high,ask_low,ask_close,volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(symbol, tf, *b) for b in bars])
            self._conn.commit()
            return len(bars)

    def get_candles(self, symbol: str, tf: int, start_ts: int | None = None,
                    end_ts: int | None = None, limit: int = 5000) -> list[Candle]:
        """标准化读取入口：按时间升序返回最多 limit 根（bid 口径）。"""
        sql = "SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND tf=?"
        args: list = [symbol, tf]
        if start_ts is not None:
            sql += " AND ts>=?"
            args.append(start_ts)
        if end_ts is not None:
            sql += " AND ts<=?"
            args.append(end_ts)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Candle(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
                for r in reversed(rows)]

    def latest_ts(self, symbol: str, tf: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
        return row[0] if row and row[0] is not None else None

    def earliest_ts(self, symbol: str, tf: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
        return row[0] if row and row[0] is not None else None

    def count(self, symbol: str, tf: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM candles WHERE symbol=? AND tf=?", (symbol, tf)).fetchone()
        return row[0] if row else 0

    # ---- 回填游标 ----

    def save_backfill_cursor(self, symbol: str, tf: int, earliest_ts: int | None,
                             done: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO backfill_cursor(symbol,tf,earliest_ts,done) "
                "VALUES(?,?,?,?)", (symbol, tf, earliest_ts, int(done)))
            self._conn.commit()

    def load_backfill_cursor(self, symbol: str, tf: int) -> tuple[int | None, bool]:
        with self._lock:
            row = self._conn.execute(
                "SELECT earliest_ts,done FROM backfill_cursor WHERE symbol=? AND tf=?",
                (symbol, tf)).fetchone()
        return (row[0], bool(row[1])) if row else (None, False)

    # ---- 净值采样 ----

    def add_equity_sample(self, env: str, balance: float, equity: float,
                          margin_used: float, ts: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_samples(env,ts,balance,equity,margin_used) "
                "VALUES(?,?,?,?,?)",
                (env, int(ts or time.time()), balance, equity, margin_used))
            self._conn.commit()

    def get_equity(self, env: str, start_ts: int | None = None,
                   end_ts: int | None = None) -> list[dict]:
        sql = "SELECT ts,balance,equity,margin_used FROM equity_samples WHERE env=?"
        args: list = [env]
        if start_ts is not None:
            sql += " AND ts>=?"
            args.append(start_ts)
        if end_ts is not None:
            sql += " AND ts<=?"
            args.append(end_ts)
        sql += " ORDER BY ts ASC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"ts": r[0], "balance": r[1], "equity": r[2], "margin_used": r[3]}
                for r in rows]

    # ---- 订单日志 ----

    def add_journal(self, env: str, order_type: str, symbol: str = "", side: str = "",
                    amount: int | None = None, requested_rate: float | None = None,
                    filled_rate: float | None = None, status: str = "",
                    sl: float | None = None, tp: float | None = None,
                    detail: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO order_journal(env,ts,order_type,symbol,side,amount,"
                "requested_rate,filled_rate,status,sl,tp,detail) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (env, int(time.time()), order_type, symbol, side, amount,
                 requested_rate, filled_rate, status, sl, tp, detail))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def get_journal(self, env: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT id,ts,env,order_type,symbol,side,amount,requested_rate," \
              "filled_rate,status,sl,tp,detail FROM order_journal"
        args: list = []
        if env is not None:
            sql += " WHERE env=?"
            args.append(env)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        keys = ["id", "ts", "env", "order_type", "symbol", "side", "amount",
                "requested_rate", "filled_rate", "status", "sl", "tp", "detail"]
        return [dict(zip(keys, r)) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
