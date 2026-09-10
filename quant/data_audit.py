"""M0 数据完备性审计（批复 0910 的验证门槛工具）。

缺口检测：相邻 bar 间隔超过 tf×max_gap_ratio 视为候选缺口，跨周末（周五→周六/日/周一）豁免。
覆盖率：实际 bar 数 / 非周日理论 bar 数（近似口径，仅作参考基线）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

SYMBOLS = ("XAU/USD", "USD/JPY", "EUR/USD")
TFS = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _is_weekend_bridge(left_ts: int, right_ts: int) -> bool:
    left = datetime.fromtimestamp(left_ts, tz=timezone.utc).weekday()
    right = datetime.fromtimestamp(right_ts, tz=timezone.utc).weekday()
    return left == 4 and right in (5, 6, 0)


def audit_symbol_tf(feed, symbol: str, tf_sec: int,
                    max_gap_ratio: float = 3.0, top: int = 5) -> dict:
    o = feed.load(symbol, tf_sec)
    if len(o) == 0:
        return {"symbol": symbol, "tf": tf_sec, "count": 0, "status": "EMPTY", "gaps": []}
    ts = o.ts
    diffs = np.diff(ts)
    gap_idx = np.flatnonzero(diffs > tf_sec * max_gap_ratio)
    gaps = [(int(ts[i]), int(ts[i + 1]), int(diffs[i]))
            for i in gap_idx
            if not _is_weekend_bridge(int(ts[i]), int(ts[i + 1]))]
    gaps.sort(key=lambda g: -g[2])

    span = int(ts[-1] - ts[0])
    days = max(1, span // 86400)
    weekend_days = sum(1 for d in range(days)
                       if datetime.fromtimestamp(int(ts[0]) + d * 86400,
                                                 tz=timezone.utc).weekday() >= 5)
    expected = max(1, int((span - weekend_days * 86400) / tf_sec))
    coverage = round(len(ts) / expected, 4)
    status = "OK" if not gaps else f"WARN({len(gaps)}缺口)"
    return {"symbol": symbol, "tf": tf_sec, "count": int(len(ts)),
            "earliest": int(ts[0]), "latest": int(ts[-1]),
            "coverage": coverage, "status": status,
            "gaps": gaps[:top], "gap_cnt": len(gaps)}
