"""engine BE 逐 bar 触发（入场快照）+ validate top-K 选格 的手算对账测试（全离线）。"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np

from quant.data import OHLCV
from quant.engine import run
from quant.validate import select_plateau_topk


def _ohlcv(bars: list[tuple[float, float, float, float]]) -> OHLCV:
    ts = np.arange(len(bars), dtype=np.int64) * 60
    return OHLCV(ts, np.array([b[0] for b in bars]), np.array([b[1] for b in bars]),
                 np.array([b[2] for b in bars]), np.array([b[3] for b in bars]),
                 np.ones(len(bars), dtype=np.int64))


class TestBeTriggerPerBar(unittest.TestCase):
    def test_trigger_snapshot_at_entry_and_scalar_compat(self):
        # band=100 恒定；入场1@bar1（trigger=0.5 → 浮盈 0.5 推保本 → bar2 回落以成本平，reason=be）
        # 入场2@bar3（trigger=50 永不触发 → eod）
        bars = [
            (99.5, 99.6, 99.4, 99.5),    # b0: 前收
            (100.0, 101.0, 99.5, 100.8),  # b1: 穿带入场@100；h≥100.5 → sl=100
            (100.5, 100.6, 99.9, 99.95),  # b2: l≤100 → 成本平（be）
            (100.0, 100.3, 99.9, 100.2),  # b3: 再入场@100；trigger=50 不触发
            (100.2, 100.4, 99.95, 100.1),  # b4: 无出场
            (100.1, 100.2, 100.0, 100.1),  # b5: eod
        ]
        data = _ohlcv(bars)
        band = np.full(len(bars), 100.0)
        tp = np.full(len(bars), 10.0)
        sl = np.full(len(bars), 2.0)
        be_arr = np.array([0.0, 0.5, 0.0, 50.0, 0.0, 0.0])
        trades = run(data, band, band, tp, sl, "TEST", direction_mode=1,
                     be_enabled=True, be_trigger=be_arr, be_buffer=0.0)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].exit_reason, "be")
        self.assertAlmostEqual(trades[0].exit_price, 100.0)
        self.assertEqual(trades[1].exit_reason, "eod")
        self.assertAlmostEqual(trades[1].entry_price, 100.0)
        self.assertAlmostEqual(trades[1].profit_target_price, 110.0)
        # 标量兼容：同样的数据 + be_trigger=0.5 标量（第 2 笔入场后 h 未达 100.5? b4 h=100.4 <100.5 不触发；
        # b3 当根 h=100.3 <100.5 不触发 → 第 2 笔仍 eod；第 1 笔 be 不变）
        trades2 = run(data, band, band, tp, sl, "TEST", direction_mode=1,
                      be_enabled=True, be_trigger=0.5, be_buffer=0.0)
        self.assertEqual(trades2[0].exit_reason, "be")
        self.assertEqual(trades2[1].exit_reason, "eod")


class TestSelectPlateauTopk(unittest.TestCase):
    def test_topk_order_and_stable_ties(self):
        sharpe = np.zeros((11, 11))
        sharpe[0:5, 0:5] = 2.0        # 高原 A（行主序在前）
        sharpe[6:11, 6:11] = 1.5      # 高原 B
        ok = np.ones_like(sharpe, dtype=bool)
        top = select_plateau_topk(sharpe, ok, k=3)
        # (0,2) 窗口 15 格全 2.0；角落 (0,0)/(0,1) 窗口 9/12 格 <13 被排除；
        # (0,3) 窗口溢出到背景 nm=1.6 → 前三 = (0,2),(1,1),(1,2)（行主序平票）
        self.assertEqual(top[0], (0, 2))
        self.assertEqual(top[1], (1, 1))
        self.assertEqual(top[2], (1, 2))

    def test_topk_excludes_unqualified(self):
        sharpe = np.array([[9.0], [1.0], [1.0]])
        ok = np.array([[False], [True], [True]])
        top = select_plateau_topk(sharpe, ok, k=5)
        self.assertEqual(top[0], (1, 0))     # 降级路径：合格格中自身 sharpe 最大

    def test_empty_returns_empty(self):
        sharpe = np.full((3, 3), np.nan)
        self.assertEqual(select_plateau_topk(sharpe, np.ones((3, 3), dtype=bool), k=2), [])


if __name__ == "__main__":
    unittest.main()
