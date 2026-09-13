"""validate 测试：build_folds 日历月切分边界 + WFA 合成数据冒烟（全离线）。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from quant.cli import parse_iso_date
from quant.data import OHLCV
from quant.validate import (build_folds, neighborhood_mean_sharpe, select_plateau_center,
                            window_valid_count)

_DAY = 86400
_MIN_TEST_SPAN = 45 * _DAY


def ts(s: str) -> int:
    return parse_iso_date(s)


def ym(t: int) -> int:
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.year * 12 + dt.month


class TestBuildFolds(unittest.TestCase):
    def test_hand_computed_24_6_over_2020_2026(self):
        # 手算：锚 2020-01-01，train 24 / test 6 → 折起点 2020-01、2020-07、…；
        # 末折 train 2024-07→2026-07，test 裁剪到 2026-09-11（72 天 ≥ 45 天）→ 共 10 折
        folds = build_folds(ts("2020-01-01"), ts("2026-09-11"), 24, 6)
        self.assertEqual(len(folds), 10)
        first = folds[0]
        self.assertEqual((first[0], first[1], first[2], first[3]),
                         (ts("2020-01-01"), ts("2022-01-01"),
                          ts("2022-01-01"), ts("2022-07-01")))
        last = folds[-1]
        self.assertEqual((last[0], last[1], last[2], last[3]),
                         (ts("2024-07-01"), ts("2026-07-01"),
                          ts("2026-07-01"), ts("2026-09-11")))

    def test_fold_invariants(self):
        folds = build_folds(ts("2020-01-01"), ts("2026-09-11"), 24, 6)
        for tr0, tr1, te0, te1 in folds:
            self.assertEqual(tr1, te0)                       # train_end == test_start
            self.assertGreaterEqual(te1 - te0, _MIN_TEST_SPAN)
        for a, b in zip(folds, folds[1:]):
            self.assertEqual(ym(b[0]) - ym(a[0]), 6)         # 步进 = test 6 个月
            self.assertEqual(ym(b[1]) - ym(a[1]), 6)

    def test_last_fold_dropped_when_clipped_span_under_45d(self):
        # test 段裁剪后 40 天 < 45 天 → 末折丢弃（仅 1 折）
        folds = build_folds(ts("2024-01-01"), ts("2026-08-10"), 24, 6)
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0][3], ts("2026-07-01"))

    def test_clipped_span_exactly_45d_kept(self):
        folds = build_folds(ts("2024-01-01"), ts("2026-08-15"), 24, 6)
        self.assertEqual(len(folds), 2)
        self.assertEqual(folds[1][3], ts("2026-08-15"))

    def test_anchor_is_day1_of_start_month(self):
        # 月末/月中起始一律锚定当月 1 日（规避月末钳位）
        folds = build_folds(ts("2020-01-15"), ts("2022-10-01"), 24, 6)
        self.assertEqual(folds[0][0], ts("2020-01-01"))
        folds = build_folds(ts("2020-03-31"), ts("2022-12-01"), 24, 6)
        self.assertEqual(folds[0][0], ts("2020-03-01"))

    def test_window_too_short_returns_empty(self):
        self.assertEqual(build_folds(ts("2026-08-01"), ts("2026-09-11"), 12, 6), [])


_VOL_REVERSAL = (Path(__file__).resolve().parents[1] / "strategies" / "vol_reversal.py")


def synth_m1_years(months: int, start: str, seed: int = 42) -> OHLCV:
    """随机游走 m1（固定种子）：o=前收，h/l 加噪声；桶对齐到 H4。"""
    rng = np.random.default_rng(seed)
    n = months * 30 * 1440
    base = ts(start)
    close = 110.0 + np.cumsum(rng.normal(0.0, 0.02, n))
    open_ = np.empty(n)
    open_[0] = 110.0
    open_[1:] = close[:-1]
    wig = np.abs(rng.normal(0.0, 0.01, n))
    ts_arr = base + np.arange(n, dtype=np.int64) * 60
    return OHLCV(ts_arr, open_, np.maximum(open_, close) + wig,
                 np.minimum(open_, close) - wig, close,
                 np.ones(n, dtype=np.int64))


class TestPlateauSelection(unittest.TestCase):
    """预提交高原选参规则：邻域均值、窗口计数、中心选取与降级路径。"""

    def test_neighborhood_mean_hand_computed(self):
        sharpe = np.array([[1.0, 2.0, 3.0],
                           [4.0, 5.0, 6.0],
                           [7.0, 8.0, 9.0]])
        nm = neighborhood_mean_sharpe(sharpe, radius=1)
        self.assertAlmostEqual(nm[1, 1], 5.0)                       # 3×3 全窗
        self.assertAlmostEqual(nm[0, 0], (1 + 2 + 4 + 5) / 4)       # 边缘窗口 4 有效格
        self.assertAlmostEqual(nm[0, 1], (1 + 2 + 3 + 4 + 5 + 6) / 6)

    def test_neighborhood_mean_ignores_nan(self):
        sharpe = np.array([[np.nan, 2.0], [4.0, 6.0]])
        nm = neighborhood_mean_sharpe(sharpe, radius=1)
        self.assertAlmostEqual(nm[1, 1], 4.0)                       # (2+4+6)/3
        self.assertAlmostEqual(nm[0, 0], 4.0)                       # 2×2 矩阵各窗口互含全部有效格

    def test_window_valid_count(self):
        valid = np.array([[True, False], [False, True]])
        cnt = window_valid_count(valid, radius=1)
        self.assertEqual(cnt[0, 0], 2)
        self.assertEqual(cnt[1, 1], 2)

    def test_plateau_beats_isolated_spike(self):
        # 11×11 背景 0；孤立尖峰 9@(2,2)；5×5 高原 2.0@(6..10,6..10) → 选高原中心 (8,8)
        sharpe = np.zeros((11, 11))
        sharpe[2, 2] = 9.0
        sharpe[6:11, 6:11] = 2.0
        trades_ok = np.ones_like(sharpe, dtype=bool)
        sel = select_plateau_center(sharpe, trades_ok)
        self.assertEqual(sel, (8, 8))

    def test_trades_ok_filter_and_empty(self):
        sharpe = np.ones((5, 5))
        self.assertIsNone(select_plateau_center(sharpe, np.zeros_like(sharpe, dtype=bool)))
        self.assertIsNone(select_plateau_center(np.full((5, 5), np.nan),
                                                np.ones_like(sharpe, dtype=bool)))

    def test_single_column_grid_falls_back(self):
        # 单维网格（n2=1）：邻域覆盖不足 → 降级为合格格中自身 sharpe 最大（v1 规则）
        sharpe = np.array([[0.1], [5.0], [0.2]])
        trades_ok = np.ones_like(sharpe, dtype=bool)
        sel = select_plateau_center(sharpe, trades_ok)
        self.assertEqual(sel, (1, 0))

    def test_tie_break_first_in_row_major_order(self):
        # 两块等值 5×5 高原 → 首个合格中心；角落格窗口仅 9/12 有效格 < 13 被排除 → (0,2)
        sharpe = np.zeros((11, 11))
        sharpe[0:5, 0:5] = 2.0
        sharpe[6:11, 6:11] = 2.0
        sel = select_plateau_center(sharpe, np.ones_like(sharpe, dtype=bool))
        self.assertEqual(sel, (0, 2))



    def test_smoke_random_walk_two_folds(self):
        # 合成 36 个月随机游走：train 24 / test 6 → 2 折；网格 3 值；松断言（完成 + 聚合行 + 参数在网格内）
        if not _VOL_REVERSAL.exists():
            self.skipTest("strategies/vol_reversal.py 不存在（不入库）")
        spec = importlib.util.spec_from_file_location("wfa_smoke_strategy", _VOL_REVERSAL)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["wfa_smoke_strategy"] = mod
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as tmp:
            from argparse import Namespace

            from fxcm_api.data.store import CandleStore

            db_path = f"{tmp}/candles.db"
            store = CandleStore(db_path)
            ohlcv = synth_m1_years(36, "2020-01-01")
            rows = [(int(ohlcv.ts[i]), float(ohlcv.open[i]), float(ohlcv.high[i]),
                     float(ohlcv.low[i]), float(ohlcv.close[i]), 1)
                    for i in range(len(ohlcv.ts))]
            store.upsert_bid_candles("USD/JPY", 60, rows)
            store.close()

            args = Namespace(
                db=db_path, strategy=str(_VOL_REVERSAL), symbol="USD/JPY",
                start="2020-01-01", end="2022-12-25", direction="long",
                quantity=50000, capital=5000.0, spread_rt=0.01,
                cost_levels="1", train_months=24, test_months=6,
                param1_start=0.2, param1_stop=0.8, param1_step=0.3,
                min_train_trades=5, warmup_days=20, out=f"{tmp}/results",
                label="", xlsx=None, sparam=[])

            from quant.validate import cmd_wfa

            self.assertEqual(cmd_wfa(args), 0)
            payload = json.loads(
                Path(f"{tmp}/results/USD_JPY_2020-01-01_2022-12-25_wfa_default.json").read_text())
            self.assertEqual(len(payload["folds"]), 2)
            agg = payload["aggregate"]
            self.assertEqual(agg["label"], "long_wfa_oos")
            grid = {0.2, 0.5, 0.8}
            for fold in payload["folds"]:
                self.assertTrue(fold["chosen_param1"] is None
                                or fold["chosen_param1"] in grid)
            for p_str, cnt in payload["param_distribution"].items():
                self.assertEqual(cnt, sum(1 for f in payload["folds"]
                                          if f["chosen_param1"] == float(p_str)))


if __name__ == "__main__":
    unittest.main()
