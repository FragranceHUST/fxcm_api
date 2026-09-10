"""回测数据源：只读 SQLite 直连（WAL 与 daemon/回填进程共存），numpy 数组形态避免百万行对象开销。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np


@dataclass
class OHLCV:
    ts: np.ndarray        # int64 epoch 秒，升序
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return int(self.ts.shape[0])


class DataFeed:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        self._conn.execute("PRAGMA busy_timeout=10000")

    def close(self) -> None:
        self._conn.close()

    def load(self, symbol: str, tf: int,
             start_ts: int | None = None, end_ts: int | None = None) -> OHLCV:
        q = "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? AND tf=?"
        args: list = [symbol, tf]
        if start_ts is not None:
            q += " AND ts>=?"
            args.append(int(start_ts))
        if end_ts is not None:
            q += " AND ts<=?"
            args.append(int(end_ts))
        q += " ORDER BY ts"
        rows = self._conn.execute(q, args).fetchall()
        if not rows:
            empty = np.empty(0)
            return OHLCV(np.empty(0, dtype=np.int64), empty, empty, empty, empty,
                         np.empty(0, dtype=np.int64))
        arr = np.asarray(rows, dtype=np.float64)
        return OHLCV(arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3],
                     arr[:, 4], arr[:, 5].astype(np.int64))

    @staticmethod
    def resample(src: OHLCV, tf_sec: int) -> OHLCV:
        """epoch 对齐桶聚合（与 fxcm_api.candles.rebuild 口径一致）。"""
        if len(src) == 0:
            return OHLCV(np.empty(0, dtype=np.int64), np.empty(0), np.empty(0),
                         np.empty(0), np.empty(0), np.empty(0, dtype=np.int64))
        bucket = (src.ts // tf_sec) * tf_sec
        idx = np.flatnonzero(np.diff(bucket, prepend=bucket[0] - 1))  # 每组首行
        last = np.append(idx[1:], len(src)) - 1                       # 每组末行
        return OHLCV(
            bucket[idx].astype(np.int64),
            src.open[idx],
            np.maximum.reduceat(src.high, idx),
            np.minimum.reduceat(src.low, idx),
            src.close[last],
            np.add.reduceat(src.volume, idx).astype(np.int64),
        )
