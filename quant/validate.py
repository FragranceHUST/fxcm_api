"""Walk-Forward Analysis：日历月滚动切分（build_folds）→ 折内网格选参 → OOS 拼接聚合。

切分口径：以 start_ts 所在月的 1 日为锚，fold k 的 train=[锚+k×test, +train)，
test=[train_end, +test)；末折 test_end 裁剪到 end_ts（排他），裁剪后 test 段
不足 45 天的折丢弃。折内每个 param1 在 train 段回测，按（sharpe 降序、
total_pnl 降序）选出平仓笔数 ≥ min_train_trades 的最优参数，再在 test 段
做样本外验证；全部 OOS 交易按时间拼接为一条聚合行。
"""
from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from quant.cli import load_strategy_class, parse_iso_date, parse_params, resolve_db
from quant.cost import CostModel
from quant.data import DataFeed, PreloadedFeed, h4_aligned_start
from quant.optimize import _csv_cell, _jsonable, _pnl_by_exit_year, _profit_factor, _row
from quant.report import append_sheet
from quant.strategy import BacktestResult, StrategyBase
from quant.trade import Trade

MIN_TEST_SPAN_SEC = 45 * 86400


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


def _fold_row(fold_id: int, window: tuple[int, int, int, int],
              chosen_param1: float | None, is_stats: dict | None,
              oos_stats: dict | None, oos_trades: list[Trade],
              capital: float) -> dict:
    train_start, train_end, test_start, test_end = window
    row: dict = {
        "fold_id": fold_id,
        "train_start": _iso_date(train_start),
        "train_end": _iso_date(train_end),
        "test_start": _iso_date(test_start),
        "test_end": _iso_date(test_end),
        "chosen_param1": chosen_param1,
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
    feed = DataFeed(db_path)
    t0 = time.perf_counter()
    ohlcv = feed.load(args.symbol, 60, cache_start, end_ts)
    feed.close()
    if len(ohlcv) == 0:
        raise SystemExit("预加载窗口内无数据，检查数据源与时间范围")
    span = (f"{datetime.fromtimestamp(int(ohlcv.ts[0]), tz=timezone.utc):%Y-%m-%d %H:%M}"
            f" → {datetime.fromtimestamp(int(ohlcv.ts[-1]), tz=timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"预加载 {len(ohlcv)} 根 m1（{span}），{len(folds)} 折 × 网格全部复用")
    preloaded = PreloadedFeed(ohlcv)

    grid = np.round(np.arange(args.param1_start, args.param1_stop + args.param1_step / 2,
                              args.param1_step), 10)
    cost_mult = float(str(args.cost_levels).split(",")[0])
    cost_model = CostModel(spread_rt=args.spread_rt * cost_mult)
    extra_params = parse_params(args.sparam)

    fold_rows: list[dict] = []
    oos_trades: list[Trade] = []
    param_count: dict[float, int] = {}
    for fold_id, window in enumerate(folds):
        train_start, train_end, test_start, test_end = window
        candidates: list[tuple[float, StrategyBase, BacktestResult]] = []
        for p in grid:
            strategy = build(params={"param1": float(p), **extra_params}, symbol=args.symbol,
                             direction_mode=args.direction, quantity=args.quantity,
                             total_capital=args.capital, cost_model=cost_model)
            res = strategy.run_backtest(preloaded, train_start, train_end - 1)
            candidates.append((float(p), strategy, res))
        qualified = [(p, s, r) for p, s, r in candidates
                     if r.stats["closed_trade_cnt"] >= args.min_train_trades]
        if not qualified:
            fold_rows.append(_fold_row(fold_id, window, None, None, None, [],
                                       args.capital))
            print(f"fold {fold_id} train {_iso_date(train_start)}→{_iso_date(train_end)} "
                  f"test {_iso_date(test_start)}→{_iso_date(test_end)}: "
                  f"无合格参数（train 平仓 < {args.min_train_trades}），跳过 OOS")
            continue
        best_p, best_strategy, best_res = max(qualified, key=lambda c: _select_key(c[2].stats))
        is_stats = best_res.stats
        oos_res = best_strategy.run_backtest(preloaded, test_start, test_end - 1)
        oos_stats = oos_res.stats
        oos_trades.extend(oos_res.trades)
        param_count[best_p] = param_count.get(best_p, 0) + 1
        fold_rows.append(_fold_row(fold_id, window, best_p, is_stats,
                                   oos_stats, oos_res.trades, args.capital))
        print(f"fold {fold_id} train {_iso_date(train_start)}→{_iso_date(train_end)} "
              f"test {_iso_date(test_start)}→{_iso_date(test_end)}: "
              f"chosen p1={best_p:.2f} is_sharpe={is_stats['sharpe_ratio']:.2f} "
              f"oos_sharpe={oos_stats['sharpe_ratio']:.2f} "
              f"oos_pnl=${oos_stats['total_pnl']:.2f} oos_trades={oos_stats['closed_trade_cnt']}")

    oos_trades.sort(key=lambda t: t.entry_time)
    oos_years = ((folds[-1][3] - folds[0][2]) / 86400 / 365.25
                 if oos_trades else None)
    agg = _row(None, cost_mult, StrategyBase.calc_cost_function(oos_trades),
               oos_trades, args.capital,
               label=f"{args.direction}_{args.label or ''}wfa_oos", years=oos_years)
    print(f"--- OOS 拼接聚合（{agg['trades']} 笔）---")
    print(f"  winrate={agg['winrate'] * 100:.1f}% pnl=${agg['total_pnl']:.2f} "
          f"ret%={agg['return_pct']:.2f} sharpe={agg['sharpe_ratio']:.2f} "
          f"maxDD=${agg['max_drawdown']:.2f} pf={agg['profit_factor']:.2f}")
    dist = {f"p1={p:.2f}": c for p, c in sorted(param_count.items())}
    print(f"参数分布: {dist or '（无折选出参数）'}")
    elapsed = time.perf_counter() - t0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_tag = args.symbol.replace("/", "_")
    base = f"{symbol_tag}_{args.start}_{args.end}_wfa"
    payload = {
        "meta": {
            "symbol": args.symbol, "strategy": args.strategy,
            "direction_mode": args.direction, "label": args.label,
            "start": args.start, "end": args.end,
            "start_ts": start_ts, "end_ts": end_ts,
            "train_months": args.train_months, "test_months": args.test_months,
            "min_train_trades": args.min_train_trades,
            "param1_start": args.param1_start, "param1_stop": args.param1_stop,
            "param1_step": args.param1_step, "grid_size": int(grid.shape[0]),
            "cost_mult": cost_mult, "spread_rt": args.spread_rt,
            "quantity": args.quantity, "capital": args.capital,
            "warmup_days": warmup_days, "cache_start_ts": cache_start,
            "folds": len(folds), "oos_folds": sum(param_count.values()),
            "bars": len(ohlcv), "db": db_path,
            "elapsed_sec": round(elapsed, 2),
        },
        "folds": [_jsonable(r) for r in fold_rows],
        "param_distribution": {str(p): c for p, c in sorted(param_count.items())},
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
                      f"selection_metric=sharpe（折内 train 网格最优，平仓 ≥ {args.min_train_trades} 笔）",
                      f"oos_aggregate_pnl={agg['total_pnl']}",
                      f"param_distribution={json.dumps({str(p): c for p, c in sorted(param_count.items())})}"]
        append_sheet(args.xlsx, f"{args.direction}_{args.label or 'wfa'}",
                     headers,
                     [[_jsonable(r)[k] for k in headers]
                      for r in sorted(fold_rows, key=lambda r: r["fold_id"])],
                     meta_lines)
        print(f"Excel 追加 sheet '{args.direction}_{args.label or 'wfa'}' → {args.xlsx}")
    print(f"完成 {len(folds)} 折（OOS {sum(param_count.values())} 折，耗时 {elapsed:.1f}s）→ {json_path}")
    print(f"CSV → {csv_path}")
    return 0
