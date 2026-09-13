"""Walk-Forward Analysis：日历月滚动切分（build_folds）→ 折内网格选参 → OOS 拼接聚合。

切分口径：以 start_ts 所在月的 1 日为锚，fold k 的 train=[锚+k×test, +train)，
test=[train_end, +test)；末折 test_end 裁剪到 end_ts（排他），裁剪后 test 段
不足 45 天的折丢弃。折内每个 (param1[, param2]) 在 train 段回测，按预提交的
「5×5 邻域均值 sharpe 最大格中心」规则选参（只使用折内 train 数据，禁止用全样本
高原限制折内搜索——泄漏），再在 test 段做样本外验证；全部 OOS 交易按时间拼接。
"""
from __future__ import annotations

import csv
import json
import math
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from quant.cli import load_strategy_class, parse_iso_date, parse_params, resolve_db
from quant.cost import CostModel
from quant.data import DataFeed, PreloadedFeed, h4_aligned_start
from quant.optimize import (_csv_cell, _jsonable, _pnl_by_exit_year, _profit_factor,
                            _row, build_grid, default_workers, result_base_name,
                            run_backtest_tasks, wfa_worker)
from quant.report import append_sheet
from quant.strategy import BacktestResult, StrategyBase
from quant.trade import Trade

MIN_TEST_SPAN_SEC = 45 * 86400
PLATEAU_RADIUS = 2                     # 5×5 邻域（p1 ±2 格 × p2 ±2 格）
PLATEAU_MIN_WINDOW_VALID = 13          # 邻域有效格数下限（25 格的过半数）


def _month_floor(ts: int) -> int:
    """锚定到 ts 所在月 UTC 1 日 00:00（月算术一律以 1 日为基准，规避月末钳位）。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp())


def _add_months(ts: int, months: int) -> int:
    """日历月加法（输入须为月内 1 日锚点）；Jan 1 + 24 个月 = 次次年 Jan 1。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    total = dt.year * 12 + (dt.month - 1) + months
    return int(datetime(total // 12, total % 12 + 1, 1, tzinfo=timezone.utc).timestamp())


def build_folds(start_ts: int, end_ts: int, train_months: int,
                test_months: int) -> list[tuple[int, int, int, int]]:
    """滚动切分，返回 (train_start, train_end, test_start, test_end)——均为排他上界的
    月对齐 epoch 秒（train_end == test_start；末折 test_end 裁剪到 end_ts）。"""
    anchor = _month_floor(start_ts)
    folds: list[tuple[int, int, int, int]] = []
    k = 0
    while True:
        train_start = _add_months(anchor, k * test_months)
        test_start = _add_months(train_start, train_months)
        test_end = min(_add_months(test_start, test_months), end_ts)
        if test_end - test_start < MIN_TEST_SPAN_SEC:
            break                              # test 段单调缩短，后续折必不达标
        folds.append((train_start, test_start, test_start, test_end))
        k += 1
    return folds


def _select_key(stats: dict) -> tuple:
    s = stats.get("sharpe_ratio")
    return (s if s is not None else -math.inf, stats["total_pnl"])


def _iso_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------- 高原选参（预提交规则，纯函数便于单测） ----------

def neighborhood_mean_sharpe(sharpe: np.ndarray, radius: int = PLATEAU_RADIUS) -> np.ndarray:
    """每格 (2r+1)×(2r+1) 邻域的 sharpe 均值（NaN 忽略、全 NaN 窗口 → NaN），输出同形。"""
    k = 2 * radius + 1
    pad = np.full((sharpe.shape[0] + 2 * radius, sharpe.shape[1] + 2 * radius), np.nan)
    pad[radius:radius + sharpe.shape[0], radius:radius + sharpe.shape[1]] = sharpe
    win = sliding_window_view(pad, (k, k))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 全 NaN 窗口的空切片均值
        with np.errstate(invalid="ignore"):
            return np.nanmean(win, axis=(-2, -1))


def window_valid_count(valid: np.ndarray, radius: int = PLATEAU_RADIUS) -> np.ndarray:
    """每格 (2r+1)×(2r+1) 窗口内的有效格数（含自身）。"""
    k = 2 * radius + 1
    pad = np.zeros((valid.shape[0] + 2 * radius, valid.shape[1] + 2 * radius), dtype=np.int32)
    pad[radius:radius + valid.shape[0], radius:radius + valid.shape[1]] = valid.astype(np.int32)
    win = sliding_window_view(pad, (k, k))
    return win.sum(axis=(-2, -1))


def select_plateau_center(sharpe: np.ndarray, trades_ok: np.ndarray,
                          min_window_valid: int = PLATEAU_MIN_WINDOW_VALID,
                          radius: int = PLATEAU_RADIUS) -> tuple[int, int] | None:
    """预提交选参：邻域均值 sharpe 最高的合格格中心；无合格格返回 None。

    合格 = 自身 sharpe 有效且平仓笔数达标（trades_ok）。邻域有效格数低于
    min_window_valid 的格子不参与；若无任何格子达标（单维网格/极端稀疏），
    降级为合格格中自身 sharpe 最大（v1 规则）。平票按 argmax 首个 =
    (p1 升序, p2 升序)。
    """
    valid = np.isfinite(sharpe) & trades_ok
    if not valid.any():
        return None
    nm = neighborhood_mean_sharpe(np.where(valid, sharpe, np.nan), radius)
    cnt = window_valid_count(valid, radius)
    ok = valid & (cnt >= min_window_valid)
    if ok.any():
        scores = np.where(ok, nm, -np.inf)
    else:
        scores = np.where(valid, sharpe, -np.inf)   # 降级：邻域覆盖不足 → 自身 sharpe 最大
    flat = int(np.argmax(scores))
    return flat // sharpe.shape[1], flat % sharpe.shape[1]


def _fold_matrix(cands: list[tuple[float, float | None, dict]], grid1: np.ndarray,
                 grid2: list[float | None],
                 min_train_trades: int) -> tuple[np.ndarray, np.ndarray]:
    """折内候选 → (sharpe 矩阵, 平仓达标掩码)；无交易格 sharpe=NaN。"""
    sharpe = np.full((len(grid1), len(grid2)), np.nan)
    trades_ok = np.zeros((len(grid1), len(grid2)), dtype=bool)
    i1 = {float(p): i for i, p in enumerate(grid1)}
    i2 = {p: j for j, p in enumerate(grid2)}
    for p1, p2, stats in cands:
        i, j = i1[float(p1)], i2[p2]
        s = stats.get("sharpe_ratio")
        if s is not None:
            sharpe[i, j] = float(s)
        trades_ok[i, j] = stats["closed_trade_cnt"] >= min_train_trades
    return sharpe, trades_ok


def _fold_row(fold_id: int, window: tuple[int, int, int, int],
              chosen_param1: float | None, chosen_param2: float | None,
              is_stats: dict | None, oos_stats: dict | None, oos_trades: list[Trade],
              capital: float) -> dict:
    train_start, train_end, test_start, test_end = window
    row: dict = {
        "fold_id": fold_id,
        "train_start": _iso_date(train_start),
        "train_end": _iso_date(train_end),
        "test_start": _iso_date(test_start),
        "test_end": _iso_date(test_end),
        "chosen_param1": chosen_param1,
        "chosen_param2": chosen_param2,
    }
    if chosen_param1 is None or is_stats is None:
        row.update({k: None for k in (
            "is_sharpe", "is_pnl", "is_trades", "oos_sharpe", "oos_pnl",
            "oos_return_pct", "oos_winrate", "oos_trades", "oos_max_drawdown",
            "oos_profit_factor", "oos_avg_holding_hours", "oos_pnl_by_year")})
        return row
    assert oos_stats is not None
    row.update({
        "is_sharpe": is_stats["sharpe_ratio"],
        "is_pnl": is_stats["total_pnl"],
        "is_trades": is_stats["closed_trade_cnt"],
        "oos_sharpe": oos_stats["sharpe_ratio"],
        "oos_pnl": oos_stats["total_pnl"],
        "oos_return_pct": oos_stats["total_pnl"] / capital * 100.0 if capital else 0.0,
        "oos_winrate": oos_stats["winrate"],
        "oos_trades": oos_stats["closed_trade_cnt"],
        "oos_max_drawdown": oos_stats["max_drawdown"],
        "oos_profit_factor": _profit_factor(oos_trades),
        "oos_avg_holding_hours": oos_stats["avg_holding_minutes"] / 60.0,
        "oos_pnl_by_year": _pnl_by_exit_year(oos_trades),
    })
    return row


def cmd_wfa(args) -> int:
    build = load_strategy_class(args.strategy)
    param2_name = getattr(args, "param2_name", None)
    workers_arg = getattr(args, "workers", 0)
    if param2_name and None in (getattr(args, "param2_start", None),
                                getattr(args, "param2_stop", None),
                                getattr(args, "param2_step", None)):
        raise SystemExit("--param2-name 需要同时给出 --param2-start/stop/step")
    extra_params: dict[str, Any] = parse_params(args.sparam)
    if param2_name and param2_name in extra_params:
        raise SystemExit(f"--sparam 与 --param2-name 冲突：{param2_name}")
    start_ts = parse_iso_date(args.start)
    end_ts = parse_iso_date(args.end) - 1          # end 排他：截至前一日最后一根 m1
    folds = build_folds(start_ts, parse_iso_date(args.end),
                        args.train_months, args.test_months)
    if not folds:
        raise SystemExit("WFA 窗口不足以切出任何折（test 段 < 45 天）")
    warmup_days = args.warmup_days
    cache_start = h4_aligned_start(folds[0][0], warmup_days)

    db_path, source = resolve_db(args.symbol, args.db)
    print(f"数据源: {db_path}（{source}）")

    grid1 = build_grid(args.param1_start, args.param1_stop, args.param1_step)
    grid2: list[float | None] = ([float(p) for p in build_grid(
        getattr(args, "param2_start"), getattr(args, "param2_stop"),
        getattr(args, "param2_step"))] if param2_name else [None])
    cost_mult = float(str(args.cost_levels).split(",")[0])
    workers = workers_arg if workers_arg > 0 else default_workers()

    # 全折 × 全网格的 train run 一次性并行（折间独立）；OOS 在主进程串行
    tasks = [(fold_id, float(p1), float(p2) if p2 is not None else None,
              train_start, train_end - 1, cost_mult)
             for fold_id, (train_start, train_end, _, _) in enumerate(folds)
             for p1 in grid1 for p2 in grid2]
    init = {"db_path": db_path, "strategy_path": args.strategy, "symbol": args.symbol,
            "direction": args.direction, "quantity": args.quantity, "capital": args.capital,
            "spread_rt": args.spread_rt, "cache_start_ts": cache_start,
            "start_ts": start_ts, "end_ts": end_ts, "param2_name": param2_name,
            "years": None, "extra_params": extra_params}
    print(f"{len(folds)} 折 × 网格 {len(grid1)}×{len(grid2)} = {len(tasks)} train run，"
          f"workers={workers}")
    t0 = time.perf_counter()
    results = run_backtest_tasks(tasks, init, wfa_worker, workers,
                                 progress_every=max(1, len(tasks) // 50))
    by_fold: dict[int, list[tuple[float, float | None, dict]]] = {}
    for fold_id, p1, p2, stats in results:
        by_fold.setdefault(fold_id, []).append((p1, p2, stats))

    feed = DataFeed(db_path)
    ohlcv = feed.load(args.symbol, 60, cache_start, end_ts)
    feed.close()
    if len(ohlcv) == 0:
        raise SystemExit("预加载窗口内无数据，检查数据源与时间范围")
    preloaded = PreloadedFeed(ohlcv)

    fold_rows: list[dict] = []
    oos_trades: list[Trade] = []
    param_count: dict[tuple[float, float | None], int] = {}
    for fold_id, window in enumerate(folds):
        train_start, train_end, test_start, test_end = window
        cands = by_fold.get(fold_id, [])
        sharpe_mat, trades_ok = _fold_matrix(cands, grid1, grid2, args.min_train_trades)
        sel = select_plateau_center(sharpe_mat, trades_ok)
        if sel is None:
            fold_rows.append(_fold_row(fold_id, window, None, None, None, None, [],
                                       args.capital))
            print(f"fold {fold_id} train {_iso_date(train_start)}→{_iso_date(train_end)} "
                  f"test {_iso_date(test_start)}→{_iso_date(test_end)}: "
                  f"无合格参数（train 平仓 < {args.min_train_trades}），跳过 OOS")
            continue
        i, j = sel
        best_p = float(grid1[i])
        best_p2 = grid2[j] if param2_name else None
        is_stats = next(s for (p1, p2, s) in cands if p1 == best_p and p2 == best_p2)
        params: dict[str, Any] = {"param1": best_p, **extra_params}
        if param2_name and best_p2 is not None:
            params[param2_name] = float(best_p2)
        strategy = build(params=params, symbol=args.symbol, direction_mode=args.direction,
                         quantity=args.quantity, total_capital=args.capital,
                         cost_model=CostModel(spread_rt=args.spread_rt * cost_mult))
        oos_res = strategy.run_backtest(preloaded, test_start, test_end - 1)
        oos_stats = oos_res.stats
        oos_trades.extend(oos_res.trades)
        param_count[(best_p, best_p2)] = param_count.get((best_p, best_p2), 0) + 1
        fold_rows.append(_fold_row(fold_id, window, best_p, best_p2, is_stats,
                                   oos_stats, oos_res.trades, args.capital))
        p2_txt = f" p2={best_p2:.2f}" if best_p2 is not None else ""
        print(f"fold {fold_id} train {_iso_date(train_start)}→{_iso_date(train_end)} "
              f"test {_iso_date(test_start)}→{_iso_date(test_end)}: "
              f"chosen p1={best_p:.2f}{p2_txt} "
              f"is_sharpe={is_stats['sharpe_ratio']:.2f} "
              f"oos_sharpe={oos_stats['sharpe_ratio']:.2f} "
              f"oos_pnl=${oos_stats['total_pnl']:.2f} "
              f"oos_trades={oos_stats['closed_trade_cnt']}")

    oos_trades.sort(key=lambda t: t.entry_time)
    oos_years = ((folds[-1][3] - folds[0][2]) / 86400 / 365.25
                 if oos_trades else None)
    agg = _row(None, None, cost_mult, StrategyBase.calc_cost_function(oos_trades),
               oos_trades, args.capital,
               label=f"{args.direction}_{args.label or ''}wfa_oos", years=oos_years)
    print(f"--- OOS 拼接聚合（{agg['trades']} 笔）---")
    print(f"  winrate={agg['winrate'] * 100:.1f}% pnl=${agg['total_pnl']:.2f} "
          f"ret%={agg['return_pct']:.2f} sharpe={agg['sharpe_ratio']:.2f} "
          f"maxDD=${agg['max_drawdown']:.2f} pf={agg['profit_factor']:.2f}")
    if param2_name:
        dist = {f"{p1:.2f}|{(f'{p2:.2f}' if p2 is not None else 'const')}": c
                for (p1, p2), c in sorted(param_count.items())}
    else:
        dist = {f"{p1:.2f}": c for (p1, _), c in sorted(param_count.items())}
    print(f"参数分布: {dist or '（无折选出参数）'}")
    elapsed = time.perf_counter() - t0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_tag = args.symbol.replace("/", "_")
    base = result_base_name(args.symbol, args.start, args.end, "wfa", args.label)
    payload = {
        "meta": {
            "symbol": args.symbol, "strategy": args.strategy,
            "direction_mode": args.direction, "label": args.label,
            "start": args.start, "end": args.end,
            "start_ts": start_ts, "end_ts": end_ts,
            "train_months": args.train_months, "test_months": args.test_months,
            "min_train_trades": args.min_train_trades,
            "param1_start": args.param1_start, "param1_stop": args.param1_stop,
            "param1_step": args.param1_step, "grid1_size": int(grid1.shape[0]),
            "param2_name": param2_name,
            "param2_start": getattr(args, "param2_start", None),
            "param2_stop": getattr(args, "param2_stop", None),
            "param2_step": getattr(args, "param2_step", None),
            "grid2_size": len(grid2) if param2_name else None,
            "selection_rule": ("5x5 邻域均值 sharpe 最大格中心（仅折内 train 数据；"
                               "平票按 p1、p2 升序）"),
            "cost_mult": cost_mult, "spread_rt": args.spread_rt,
            "quantity": args.quantity, "capital": args.capital,
            "warmup_days": warmup_days, "cache_start_ts": cache_start,
            "workers": workers,
            "folds": len(folds), "oos_folds": sum(param_count.values()),
            "bars": len(ohlcv), "db": db_path,
            "elapsed_sec": round(elapsed, 2),
        },
        "folds": [_jsonable(r) for r in fold_rows],
        "param_distribution": dist,
        "aggregate": _jsonable(agg),
    }
    json_path = out_dir / f"{base}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    csv_path = out_dir / f"{base}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(list(fold_rows[0].keys()))
        for r in fold_rows:
            writer.writerow([_csv_cell(_jsonable(r)[k]) for k in fold_rows[0]])
    if args.xlsx:
        headers = list(fold_rows[0].keys())
        meta_lines = [f"symbol={args.symbol}", f"strategy={args.strategy}",
                      f"direction_mode={args.direction}", f"label={args.label}",
                      f"window={args.start}→{args.end}",
                      f"selection=折内 5x5 邻域均值 sharpe 选参（平仓 ≥ {args.min_train_trades} 笔，"
                      f"仅 train 数据）",
                      f"oos_aggregate_pnl={agg['total_pnl']}",
                      f"param_distribution={json.dumps(dist, ensure_ascii=False)}"]
        append_sheet(args.xlsx, f"{args.direction}_{args.label or 'wfa'}",
                     headers,
                     [[_jsonable(r)[k] for k in headers]
                      for r in sorted(fold_rows, key=lambda r: r["fold_id"])],
                     meta_lines)
        print(f"Excel 追加 sheet '{args.direction}_{args.label or 'wfa'}' → {args.xlsx}")
    print(f"完成 {len(folds)} 折（OOS {sum(param_count.values())} 折，耗时 {elapsed:.1f}s）→ {json_path}")
    print(f"CSV → {csv_path}")
    return 0
