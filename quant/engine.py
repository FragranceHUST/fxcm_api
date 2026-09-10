"""撮合引擎：numba 逐 m1 bar 状态机（触价制，批复 D2/B2/B3）。

语义（全部为已批复锁定项）：
- 入场：前收在带外、当前收穿回带内 → 按带沿理想价成交（B3；成本单列不进成交价）
- 持仓：TP/SL 触价即平（D2）；同根双触 SL 优先（B2）；入场当根同样检查退出（保守）
- 保本：极值触及 entry+be_trigger 后 SL 单调移到 entry+be_buffer
- 单向单持仓（D4）；band NaN（预热期）不交易；期末未平仓按最后收盘价强平（eod）

内核无 numba 时自动退化为纯 Python（同代码，慢但正确）。
"""
from __future__ import annotations

import numpy as np

from quant.data import OHLCV
from quant.trade import Direction, Trade

try:
    from numba import njit
except ImportError:          # 纯 Python 兜底（B1 的降级路径）
    def njit(*args, **kwargs):
        deco = args[0] if args and callable(args[0]) else None
        def wrap(f):
            return f
        return wrap if deco is None else deco


@njit(cache=True)
def run_kernel(ts, open_, high, low, close, band_upper, band_lower, tp_dist, sl_dist,
               direction_mode, be_enabled, be_trigger, be_buffer,
               o_dir, o_entry_i, o_exit_i, o_entry_px, o_exit_px, o_tp, o_sl,
               o_reason, o_mfe, o_mae):
    """逐 bar 状态机。返回成交笔数；结果写入预分配的 o_* 数组。"""
    n = ts.shape[0]
    count = 0
    pos = 0
    entry_px = 0.0
    tp = 0.0
    sl = 0.0
    entry_i = -1
    mfe = 0.0
    mae = 0.0
    for i in range(n):
        o = open_[i]
        h = high[i]
        l = low[i]
        c = close[i]
        bu = band_upper[i]
        bl = band_lower[i]
        if pos == 0:
            if i == 0 or bl != bl or bu != bu or tp_dist[i] != tp_dist[i] \
                    or sl_dist[i] != sl_dist[i]:
                continue
            prev_c = close[i - 1]
            long_sig = direction_mode != 2 and prev_c < bl and c >= bl
            short_sig = direction_mode != 1 and prev_c > bu and c <= bu
            if long_sig:
                pos = 1
                entry_px = bl if l <= bl <= h else o
                entry_i = i
                tp = entry_px + tp_dist[i]
                sl = entry_px - sl_dist[i]
                mfe = 0.0
                mae = 0.0
            elif short_sig:
                pos = -1
                entry_px = bu if l <= bu <= h else o
                entry_i = i
                tp = entry_px - tp_dist[i]
                sl = entry_px + sl_dist[i]
                mfe = 0.0
                mae = 0.0
        if pos == 1:
            fav = h - entry_px
            adv = entry_px - l
            if fav > mfe:
                mfe = fav
            if adv > mae:
                mae = adv
            exit_px = 0.0
            reason = 0
            if l <= sl:
                exit_px = sl
                reason = 2                       # sl（含保本位）
            elif h >= tp:
                exit_px = tp
                reason = 1                       # tp
            if reason != 0:
                o_dir[count] = 1
                o_entry_i[count] = entry_i
                o_exit_i[count] = i
                o_entry_px[count] = entry_px
                o_exit_px[count] = exit_px
                o_tp[count] = tp
                o_sl[count] = sl
                o_reason[count] = reason
                o_mfe[count] = mfe
                o_mae[count] = mae
                count += 1
                pos = 0
            elif be_enabled and h >= entry_px + be_trigger \
                    and sl < entry_px + be_buffer:
                sl = entry_px + be_buffer
        elif pos == -1:
            fav = entry_px - l
            adv = h - entry_px
            if fav > mfe:
                mfe = fav
            if adv > mae:
                mae = adv
            exit_px = 0.0
            reason = 0
            if h >= sl:
                exit_px = sl
                reason = 2
            elif l <= tp:
                exit_px = tp
                reason = 1
            if reason != 0:
                o_dir[count] = -1
                o_entry_i[count] = entry_i
                o_exit_i[count] = i
                o_entry_px[count] = entry_px
                o_exit_px[count] = exit_px
                o_tp[count] = tp
                o_sl[count] = sl
                o_reason[count] = reason
                o_mfe[count] = mfe
                o_mae[count] = mae
                count += 1
                pos = 0
            elif be_enabled and l <= entry_px - be_trigger \
                    and sl > entry_px - be_buffer:
                sl = entry_px - be_buffer
    if pos != 0:
        i = n - 1
        exit_px = close[i]
        o_dir[count] = pos
        o_entry_i[count] = entry_i
        o_exit_i[count] = i
        o_entry_px[count] = entry_px
        o_exit_px[count] = exit_px
        o_tp[count] = tp
        o_sl[count] = sl
        o_reason[count] = 3                      # eod
        o_mfe[count] = mfe
        o_mae[count] = mae
        count += 1
    return count


REASON_MAP = {1: "tp", 2: "sl", 3: "eod"}


def run(data: OHLCV, band_upper: np.ndarray, band_lower: np.ndarray,
        tp_dist: np.ndarray, sl_dist: np.ndarray, instrument: str,
        direction_mode: int = 3, be_enabled: bool = False,
        be_trigger: float = 0.0, be_buffer: float = 0.0,
        cost_per_trade: float = 0.35, unit_value: float = 1.0,
        quantity: int = 1, param_set_id: str = "") -> list[Trade]:
    """执行回测并包装为 Trade 列表（profit 已按单位价值折算并扣除成本）。"""
    n = len(data.ts)
    max_trades = n // 2 + 2
    out = {k: np.zeros(max_trades, dtype=np.float64)
           for k in ("dir", "entry_i", "exit_i", "entry_px", "exit_px", "tp", "sl",
                     "reason", "mfe", "mae")}
    count = run_kernel(data.ts, data.open, data.high, data.low, data.close,
                       band_upper, band_lower, tp_dist, sl_dist,
                       direction_mode, be_enabled, be_trigger, be_buffer,
                       out["dir"], out["entry_i"], out["exit_i"], out["entry_px"],
                       out["exit_px"], out["tp"], out["sl"], out["reason"],
                       out["mfe"], out["mae"])
    trades: list[Trade] = []
    for k in range(count):
        d = int(out["dir"][k])
        ei, xi = int(out["entry_i"][k]), int(out["exit_i"][k])
        entry_px, exit_px = float(out["entry_px"][k]), float(out["exit_px"][k])
        raw = (exit_px - entry_px) if d == 1 else (entry_px - exit_px)
        profit = raw * unit_value * quantity - cost_per_trade * unit_value * quantity
        reason = REASON_MAP[int(out["reason"][k])]
        if reason == "sl" and abs(exit_px - entry_px) < 1e-9:
            reason = "be"
        trades.append(Trade(
            trade_id=k + 1, instrument=instrument,
            direction=Direction.ASK if d == 1 else Direction.BID,
            entry_price=entry_px, exit_price=exit_px,
            stoploss_price=float(out["sl"][k]), profit_target_price=float(out["tp"][k]),
            quantity=quantity, entry_time=int(data.ts[ei]), exit_time=int(data.ts[xi]),
            profit=profit, closed=True, exit_reason=reason, param_set_id=param_set_id,
            mfe=float(out["mfe"][k]), mae=float(out["mae"][k]),
        ))
    return trades
