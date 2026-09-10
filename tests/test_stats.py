"""stats.py 单测：CostFunction 指标口径。"""

import json
import unittest
from datetime import datetime, timezone

from fxcm_api.data.stats import compute_stats


def _t(pl, day, commission=0.0):
    o = datetime(2026, 9, int(day), 10, 0, tzinfo=timezone.utc)
    return {"gross_pl": pl, "open_time": o, "close_time": o, "commission": commission,
            "open_rate": 1.0, "close_rate": 1.0}


class TestStats(unittest.TestCase):
    def test_winrate_and_streaks(self):
        trades = [_t(100, 1), _t(-50, 1), _t(80, 2), _t(60, 2), _t(-30, 2), _t(20, 3)]
        s = compute_stats(closed_trades=trades)
        self.assertEqual(s["closed_trade_cnt"], 6)
        self.assertEqual(s["winrate"], round(4 / 6, 4))
        self.assertEqual(s["max_consecutive_profit"], 2)
        self.assertEqual(s["max_consecutive_loss"], 1)
        self.assertEqual(s["realized_pnl"], 180.0)
        self.assertEqual(s["total_commission"], 0.0)

    def test_daily_pnl_grouping(self):
        trades = [_t(100, 1), _t(-50, 1), _t(80, 2)]
        s = compute_stats(closed_trades=trades)
        self.assertEqual(s["daily_pnl"]["2026-09-01"], 50.0)
        self.assertEqual(s["daily_pnl"]["2026-09-02"], 80.0)

    def test_sharpe_zero_when_flat(self):
        trades = [_t(10, 1), _t(10, 2), _t(10, 3)]
        s = compute_stats(closed_trades=trades)
        self.assertEqual(s["sharpe_ratio"], 0.0)

    def test_max_drawdown_from_equity_curve(self):
        curve = [{"equity": 100}, {"equity": 150}, {"equity": 90}, {"equity": 120}]
        s = compute_stats(closed_trades=[], equity_curve=curve)
        self.assertEqual(s["max_drawdown"], 60.0)   # 峰150 → 谷90

    def test_slippage_and_errors_from_journal(self):
        journal = [
            {"order_type": "market", "requested_rate": 100.0, "filled_rate": 100.5},
            {"order_type": "market", "requested_rate": 200.0, "filled_rate": 199.0},
            {"order_type": "cancel", "requested_rate": 1.0, "filled_rate": 1.0},
        ]
        s = compute_stats(closed_trades=[], journal=journal)
        self.assertEqual(s["total_slippage"], 0.5 - 1.0)
        self.assertEqual(s["absolute_error"], 1.5)
        self.assertAlmostEqual(s["mean_square_error"], (0.25 + 1.0) / 2)

    def test_unrealized_and_total(self):
        s = compute_stats(closed_trades=[_t(100, 1)],
                          open_trades=[{"gross_pl": 25.5}])
        self.assertEqual(s["realized_pnl"], 100.0)
        self.assertEqual(s["unrealized_pnl"], 25.5)
        self.assertEqual(s["total_pnl"], 125.5)
        self.assertEqual(s["closed_trade_cnt"], 1)
        self.assertEqual(s["trade_cnt"], 2)   # 已平仓 + 未平仓

    def test_all_wins_ratio_is_null_and_serializable(self):
        trades = [_t(100, 1), _t(300, 2)]
        s = compute_stats(closed_trades=trades)
        self.assertIsNone(s["max_profitloss_ratio"])
        json.dumps(s, allow_nan=False)

    def test_mixed_ratio(self):
        trades = [_t(100, 1), _t(-25, 1)]
        s = compute_stats(closed_trades=trades)
        self.assertEqual(s["max_profitloss_ratio"], 4.0)

    def test_non_finite_floats_sanitized(self):
        s = compute_stats(closed_trades=[], open_trades=[{"gross_pl": float("nan")}])
        self.assertIsNone(s["unrealized_pnl"])
        self.assertIsNone(s["total_pnl"])
        json.dumps(s, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
