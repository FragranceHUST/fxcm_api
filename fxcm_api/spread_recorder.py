"""点差记录器：周期采样行情快照，按日追加 CSV，为量化成本模型积累点差分布。

零 FXCM 依赖：只读 tick provider 给出的内存快照；采样异常永不向上抛（守护线程语义）。
输出：<out_dir>/spread_YYYYMMDD.csv（UTC 日切），列见 HEADER。
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Callable
from io import TextIOWrapper
from pathlib import Path

logger = logging.getLogger("fxcm_api.spread_recorder")

HEADER = ["ts", "symbol", "bid", "ask", "spread_price", "spread_pips"]


def default_pip_size(symbol: str) -> float:
    """仓库口径：JPY 报价 0.01、其余 0.0001（金属等特殊品种经 overrides 覆盖）。"""
    return 0.01 if symbol.endswith("/JPY") else 0.0001


def spread_row(ts: float, symbol: str, bid: float, ask: float,
               pip: float) -> dict[str, float | str]:
    spread = ask - bid
    return {"ts": ts, "symbol": symbol, "bid": bid, "ask": ask,
            "spread_price": spread, "spread_pips": spread / pip}


class SpreadRecorder:
    """tick_provider 返回 {symbol: (bid, ask, tick_ts)}；过期 tick 跳过（休市不灌噪声）。"""

    def __init__(self, tick_provider: Callable[[], dict[str, tuple[float, float, float]]],
                 out_dir: Path, interval_sec: float = 5.0,
                 pip_overrides: dict[str, float] | None = None,
                 max_tick_age_sec: float = 120.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._provider = tick_provider
        self._out_dir = Path(out_dir)
        self._interval = max(1.0, float(interval_sec))
        self._overrides = dict(pip_overrides or {})
        self._max_age = float(max_tick_age_sec)
        self._clock = clock
        self._fh: TextIOWrapper | None = None
        self._writer: csv.DictWriter | None = None
        self._day = ""

    def pip_for(self, symbol: str) -> float:
        for key, value in self._overrides.items():
            if symbol.startswith(key):
                return float(value)
        return default_pip_size(symbol)

    def _rotate_if_needed(self, day: str) -> None:
        if day == self._day and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        self._out_dir.mkdir(parents=True, exist_ok=True)
        path = self._out_dir / f"spread_{day}.csv"
        new_file = not path.exists()
        self._fh = path.open("a", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=HEADER)
        if new_file:
            self._writer.writeheader()
        self._day = day

    def sample_once(self) -> list[dict[str, float | str]]:
        now = self._clock()
        self._rotate_if_needed(time.strftime("%Y%m%d", time.gmtime(now)))
        rows: list[dict[str, float | str]] = []
        for symbol, (bid, ask, tick_ts) in self._provider().items():
            if now - tick_ts > self._max_age:
                continue
            rows.append(spread_row(now, symbol, bid, ask, self.pip_for(symbol)))
        if rows and self._writer is not None and self._fh is not None:
            self._writer.writerows(rows)
            self._fh.flush()
        return rows

    def run_forever(self) -> None:
        logger.info("点差记录器启动（间隔 %.0fs，输出 %s）", self._interval, self._out_dir)
        while True:
            try:
                self.sample_once()
            except Exception:
                logger.exception("点差采样失败（忽略，继续）")
                self.close()
            time.sleep(self._interval)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
            self._day = ""
