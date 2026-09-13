"""data_audit 周末桥豁免边界：短桥豁免、多日断档必须上报（官方源缺周实测教训）。"""
import unittest
from datetime import datetime, timezone

from quant.data_audit import _is_weekend_bridge


def _ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


class TestWeekendBridge(unittest.TestCase):
    def test_normal_weekend_exempt(self):
        # 周五 21:58 → 周一 00:30（约 2.1 天）：正常周末
        self.assertTrue(_is_weekend_bridge(_ts("2024-12-06 21:58"), _ts("2024-12-09 00:30")))

    def test_multi_week_hole_not_exempt(self):
        # 周五 → 9 天后的周日：多周断档，不得豁免
        self.assertFalse(_is_weekend_bridge(_ts("2023-06-30 20:58"), _ts("2023-07-09 21:16")))

    def test_month_long_hole_not_exempt(self):
        # 周五 → 30 天后的周日：2024-12 官方源缺周复现
        self.assertFalse(_is_weekend_bridge(_ts("2024-12-13 21:58"), _ts("2025-01-12 22:36")))


if __name__ == "__main__":
    unittest.main()
