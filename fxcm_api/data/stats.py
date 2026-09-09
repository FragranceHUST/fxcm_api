"""CostFunction 14 指标移植 + 按日/总 PnL（依赖已平仓交易 + 净值采样 + 订单日志）。

口径：
- 收益序列 = 按日聚合的已实现 PnL（含未平仓浮动盈亏计入当日 equity 变动可选，此处用已实现口径）
- 夏普 = 日收益均值 / 日收益标准差 × sqrt(252)（年化），基准 0（信息比率=同口径）
- 最大回撤 = 净值曲线（优先 equity_samples，缺失时用已平仓累计 PnL 重建）
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone


def _daily_pnl(closed_trades: list[dict]) -> dict[str, float]:
    by_day: dict[str, float] = defaultdict(float)
    for t in closed_trades:
        day = _day_of(t)
        by_day[day] += float(t.get("gross_pl") or 0.0)
    return dict(sorted(by_day.items()))


def _day_of(t: dict) -> str:
    v = t.get("close_time") or t.get("open_time")
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v or "unknown")[:10]


def compute_stats(closed_trades: list[dict], open_trades: list[dict] | None = None,
                  equity_curve: list[dict] | None = None,
                  journal: list[dict] | None = None) -> dict:
    """CostFunction 全量指标。closed_trades 字段: gross_pl, open_time, close_time, commission。"""
    open_trades = open_trades or []
    equity_curve = equity_curve or []
    journal = journal or []
    pls = [float(t.get("gross_pl") or 0.0) for t in closed_trades]
    closed_cnt = len(pls)
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p < 0]

    winrate = len(wins) / closed_cnt if closed_cnt else 0.0
    total_pnl = sum(pls)
    realized = total_pnl
    unrealized = sum(float(t.get("gross_pl") or 0.0) for t in open_trades)

    max_profit = max(pls) if pls else 0.0
    max_loss_abs = abs(min(pls)) if losses else 0.0
    max_pl_ratio = (max_profit / max_loss_abs) if max_loss_abs > 0 else \
        (math.inf if max_profit > 0 else 0.0)

    max_con_profit = _max_consecutive(lambda p: p > 0, pls)
    max_con_loss = _max_consecutive(lambda p: p < 0, pls)

    daily = _daily_pnl(closed_trades)
    sharpe = _sharpe(list(daily.values()))
    info_ratio = sharpe                       # 基准 0（已批复）
    max_drawdown = _max_drawdown(equity_curve, closed_trades)

    holding = [_holding_seconds(t) for t in closed_trades]
    avg_holding = sum(holding) / len(holding) if holding else 0.0

    commissions = sum(float(t.get("commission") or 0.0) for t in closed_trades)
    slippage = _total_slippage(journal)

    return {
        "closed_trade_cnt": closed_cnt,
        "trade_cnt": closed_cnt + len(open_trades),
        "open_trade_cnt": len(open_trades),
        "winrate": round(winrate, 4),
        "sharpe_ratio": round(sharpe, 4),
        "information_ratio": round(info_ratio, 4),
        "max_profitloss_ratio": round(max_pl_ratio, 4),
        "max_drawdown": round(max_drawdown, 2),
        "max_consecutive_profit": max_con_profit,
        "max_consecutive_loss": max_con_loss,
        "mean_square_error": round(_mse(journal), 4),
        "absolute_error": round(_abs_error(journal), 4),
        "total_commission": round(commissions, 2),
        "total_slippage": round(slippage, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "daily_pnl": daily,
        "avg_holding_minutes": round(avg_holding / 60.0, 1),
    }


def _max_consecutive(pred, values: list[float]) -> int:
    best = cur = 0
    for v in values:
        if pred(v):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _sharpe(daily_values: list[float]) -> float:
    """日收益序列的年化夏普（基准 0；无波动/无数据返回 0）。"""
    if len(daily_values) < 2:
        return 0.0
    values = daily_values
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(252)


def _max_drawdown(equity_curve: list[dict], closed_trades: list[dict]) -> float:
    series: list[float] = []
    if len(equity_curve) >= 2:
        series = [float(e.get("equity") or 0.0) for e in equity_curve]
    elif closed_trades:
        # 用已平仓交易按时间累计重建（近似净值曲线）
        ordered = sorted(closed_trades, key=_day_of)
        base, cum = 0.0, 0.0
        series = []
        for t in ordered:
            cum += float(t.get("gross_pl") or 0.0)
            series.append(base + cum)
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def _holding_seconds(t: dict) -> float:
    o, c = t.get("open_time"), t.get("close_time")
    if hasattr(o, "timestamp") and hasattr(c, "timestamp"):
        try:
            return max(0.0, c.timestamp() - o.timestamp())
        except Exception:
            return 0.0
    return 0.0


def _slippage_points(journal: list[dict]) -> list[float]:
    out = []
    for r in journal:
        req, filled = r.get("requested_rate"), r.get("filled_rate")
        if req is not None and filled is not None and r.get("order_type") == "market":
            out.append(float(filled) - float(req))
    return out


def _total_slippage(journal: list[dict]) -> float:
    return sum(_slippage_points(journal))


def _mse(journal: list[dict]) -> float:
    pts = _slippage_points(journal)
    if not pts:
        return 0.0
    return sum(p * p for p in pts) / len(pts)


def _abs_error(journal: list[dict]) -> float:
    pts = _slippage_points(journal)
    return sum(abs(p) for p in pts)
