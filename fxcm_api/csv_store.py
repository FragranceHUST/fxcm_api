"""本地 CSV 存储：每品种一个 1s K线文件，追加写 + 启动加载恢复。

文件格式（bid 口径）：ts,open,high,low,close,volume
文件名：data/<品种>_1s.csv（品种中的 "/" 替换为 "_"）
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

from .candles import Candle

_HEADER = ["ts", "open", "high", "low", "close", "volume"]


def csv_path_for(data_dir: str | Path, symbol: str) -> Path:
    """品种名 → CSV 路径：'XAU/USD' → data/XAU_USD_1s.csv"""
    safe = symbol.replace("/", "_")
    return Path(data_dir) / f"{safe}_1s.csv"


class CsvTickStore:
    """追加式 1s K线存储。1s 桶关闭即落盘（约每秒一行），崩溃最多丢一根未收盘的K线。"""

    def __init__(self, data_dir: str | Path, symbol: str):
        self.path = csv_path_for(data_dir, symbol)
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None

    def load(self) -> list[Candle]:
        """读取全部历史K线（daemon 启动时调用一次）。"""
        if not self.path.is_file():
            return []
        out: list[Candle] = []
        with self.path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    out.append(Candle(
                        ts=int(row["ts"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row["volume"]),
                    ))
                except (KeyError, ValueError):
                    continue  # 跳过损坏行
        return out

    def open_for_append(self) -> None:
        """创建目录并打开追加句柄。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.is_file() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(_HEADER)
            self._fh.flush()

    def append(self, candle: Candle) -> None:
        """追加一行已收盘的 1s K线并立即 flush（行频 ≈1/s，代价可忽略）。"""
        with self._lock:
            if self._writer is None:
                return  # 未 open_for_append，静默忽略（只读模式）
            self._writer.writerow([candle.ts, candle.open, candle.high,
                                   candle.low, candle.close, candle.volume])
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None
                self._writer = None
