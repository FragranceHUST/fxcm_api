"""pip 引擎：按报价小数位自适应，支持按品种前缀覆盖（对标 MQL4 版 PipSize）。"""

from __future__ import annotations

from collections.abc import Mapping


def pip_size(symbol: str, point_size: float, digits: int,
             overrides: Mapping[str, float] | None = None) -> float:
    if overrides:
        for key, value in overrides.items():
            if symbol.startswith(key):
                return float(value)
    if digits in (3, 5):
        return point_size * 10.0
    return point_size
