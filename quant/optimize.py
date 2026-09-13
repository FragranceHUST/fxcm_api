"""参数扫描：param1 × 成本压力网格 + buy-hold 基准对照，结果落 JSON/CSV/Excel。

行情只查一次（含预热窗，H4 桶对齐），经 PreloadedFeed 在全部 run 间复用，
避免 153 次 × 百万行级 SQL。指标复用 compute_stats 口径，另算盈亏因子、
平均持仓小时与按出场年份的已实现 PnL。
"""
from __future__ import annotations

import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from quant.cli import load_strategy_class, parse_iso_date, resolve_db
from quant.cost import CostModel
from quant.data import BacktestFeed, DataFeed, PreloadedFeed, h4_aligned_start
from quant.report import append_sheet
from quant.strategy import StrategyBase
from quant.trade import Direction, Trade


def _profit_factor(trades: list[Trade]) -> float:
    gains = sum(t.profit for t in trades if t.profit > 0)
    losses = sum(t.profit for t in trades if t.profit < 0)
    if losses:
        return gains / abs(losses)
    return math.inf if gains > 0 else 0.0


def _pnl_by_exit_year(trades: list[Trade]) -> dict[str, float]:
    by_year: dict[str, float] = {}
    for t in trades:
        year = str(datetime.fromtimestamp(t.exit_time, tz=timezone.utc).year)
        by_year[year] = by_year.get(year, 0.0) + t.profit
    return {k: round(v, 2) for k, v in sorted(by_year.items())}


def _annualized_pct(total_pnl: float, capital: float, years: float | None) -> float | None:
    """CAGR 年化收益（复利口径，%）；净值归零/窗口无效返回 None。"""
    if not years or years <= 0 or not capital:
        return None
    multiple = 1.0 + total_pnl / capital
    if multiple <= 0:
        return None
    return (multiple ** (1.0 / years) - 1.0) * 100.0


def _row(param1: float | None, cost_mult: float | None, stats: dict,
         trades: list[Trade], capital: float, label: str = "sweep",
         years: float | None = None) -> dict:
    return {
        "label": label,
        "param1": param1,
        "cost_mult": cost_mult,
        "trades": stats["closed_trade_cnt"],
        "winrate": stats["winrate"],
        "total_pnl": stats["total_pnl"],
        "return_pct": stats["total_pnl"] / capital * 100.0 if capital else 0.0,
        "annualized_return_pct": _annualized_pct(stats["total_pnl"], capital, years),
        "max_drawdown": stats["max_drawdown"],
        "sharpe_ratio": stats["sharpe_ratio"],
        "profit_factor": _profit_factor(trades),
        "avg_holding_hours": stats["avg_holding_minutes"] / 60.0,
        "pnl_by_year": _pnl_by_exit_year(trades),
        "trade_cnt": stats["trade_cnt"],
        "open_trade_cnt": stats["open_trade_cnt"],
        "information_ratio": stats["information_ratio"],
        "max_consecutive_profit": stats["max_consecutive_profit"],
        "max_consecutive_loss": stats["max_consecutive_loss"],
        "total_commission": stats["total_commission"],
        "total_slippage": stats["total_slippage"],
        "mean_square_error": stats["mean_square_error"],
        "absolute_error": stats["absolute_error"],
        "realized_pnl": stats["realized_pnl"],
        "unrealized_pnl": stats["unrealized_pnl"],
    }


def _sharpe_sort_key(row: dict) -> tuple:
    """sharpe DESC 排序键；None（无日收益方差）排最后。"""
    s = row.get("sharpe_ratio")
    return (s is not None, s if s is not None else 0.0)


def _benchmark_pnl_by_year(w, quantity: int) -> dict[str, float]:
    """buy-hold 按年归因用逐日盯市：各年 PnL = 年末浮动 PnL − 上年末浮动 PnL（含退出年）。"""
    entry = float(w.open[0])
    fl = (w.close - entry) * quantity / w.close
    y0 = datetime.fromtimestamp(int(w.ts[0]), tz=timezone.utc).year
    y1 = datetime.fromtimestamp(int(w.ts[-1]), tz=timezone.utc).year
    bounds = [int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp())
              for y in range(y0, y1 + 2)]
    idx = np.searchsorted(w.ts, bounds)
    out: dict[str, float] = {}
    for k, y in enumerate(range(y0, y1 + 1)):
        i0, i1 = max(idx[k] - 1, 0), max(idx[k + 1] - 1, 0)
        out[str(y)] = round(float(fl[i1] - fl[i0]), 2)
    return out


def _benchmark_row(feed: BacktestFeed, symbol: str, start_ts: int, end_ts: int,
                   spread_rt: float, quantity: int, capital: float,
                   years: float | None = None) -> dict:
    """buy-hold 基准：窗口首根开盘买入、末根收盘强平，成本按一次往返。"""
    w = feed.load(symbol, 60, start_ts, end_ts)
    if len(w) == 0:
        raise SystemExit("基准窗口内无数据，检查数据源与时间范围")
    entry_px, exit_px = float(w.open[0]), float(w.close[-1])
    uv = 1.0 / exit_px
    profit = (exit_px - entry_px - spread_rt) * uv * quantity
    trade = Trade(trade_id=1, instrument=symbol, direction=Direction.ASK,
                  entry_price=entry_px, exit_price=exit_px, quantity=quantity,
                  entry_time=int(w.ts[0]), exit_time=int(w.ts[-1]),
                  profit=profit, closed=True, exit_reason="eod")
    row = _row(None, None, StrategyBase.calc_cost_function([trade]), [trade],
               capital, label="benchmark_buy_hold", years=years)
    row["pnl_by_year"] = _benchmark_pnl_by_year(w, quantity)   # MTM 按年归因，覆盖平仓年归因
    return row


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = None if isinstance(v, float) and not math.isfinite(v) else v
    return out


def _print_top(rows: list[dict], mult: float, n: int = 5) -> None:
    top = sorted((r for r in rows if r["cost_mult"] == mult),
                 key=lambda r: r["total_pnl"], reverse=True)[:n]
    for rank, r in enumerate(top, 1):
        print(f"  {rank}. param1={r['param1']:.2f} trades={r['trades']} "
              f"winrate={r['winrate'] * 100:.1f}% pnl=${r['total_pnl']:.2f} "
              f"ret%={r['return_pct']:.2f} sharpe={r['sharpe_ratio']:.2f} "
              f"maxDD=${r['max_drawdown']:.2f}")


def _print_benchmark(bench: dict) -> None:
    print(f"  benchmark_buy_hold: trades={bench['trades']} "
          f"winrate={bench['winrate'] * 100:.1f}% pnl=${bench['total_pnl']:.2f} "
          f"ret%={bench['return_pct']:.2f} sharpe={bench['sharpe_ratio']:.2f} "
          f"maxDD=${bench['max_drawdown']:.2f}")


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return "" if not math.isfinite(v) else f"{v:.6f}"
    return str(v)


def cmd_sweep(args) -> int:
    build = load_strategy_class(args.strategy)
    start_ts = parse_iso_date(args.start)
    end_ts = parse_iso_date(args.end) - 1          # end 排他：截至前一日最后一根 m1
    warmup_days = args.warmup_days                 # 须与策略默认预热一致（20 天）
    cache_start = h4_aligned_start(start_ts, warmup_days)

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
    print(f"预加载 {len(ohlcv)} 根 m1（{span}），全部 run 复用")
    preloaded = PreloadedFeed(ohlcv)

    grid = np.round(np.arange(args.param1_start, args.param1_stop + args.param1_step / 2,
                              args.param1_step), 10)
    cost_levels = [float(x) for x in str(args.cost_levels).split(",") if x.strip()]
    years = (end_ts - start_ts) / 86400 / 365.25

    rows: list[dict] = []
    for mult in cost_levels:
        cost_model = CostModel(spread_rt=args.spread_rt * mult)
        print(f"=== 成本 ×{mult:g}（spread_rt={args.spread_rt * mult:g}）===")
        for p in grid:
            strategy = build(params={"param1": float(p)}, symbol=args.symbol,
                             direction_mode=args.direction, quantity=args.quantity,
                             total_capital=args.capital, cost_model=cost_model)
            result = strategy.run_backtest(preloaded, start_ts, end_ts)
            row = _row(float(p), mult, result.stats, result.trades, args.capital,
                       years=years)
            rows.append(row)
            print(f"  param1={p:.2f} trades={row['trades']} "
                  f"winrate={row['winrate'] * 100:.1f}% pnl=${row['total_pnl']:.2f}")
        _print_top(rows, mult)

    bench = _benchmark_row(preloaded, args.symbol, start_ts, end_ts,
                           args.spread_rt, args.quantity, args.capital, years=years)
    _print_benchmark(bench)
    elapsed = time.perf_counter() - t0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_tag = args.symbol.replace("/", "_")
    base = f"{symbol_tag}_{args.start}_{args.end}_sweep"
    payload = {
        "meta": {
            "symbol": args.symbol, "strategy": args.strategy,
            "direction_mode": args.direction, "label": args.label,
            "start": args.start, "end": args.end,
            "start_ts": start_ts, "end_ts": end_ts,
            "warmup_days": warmup_days, "cache_start_ts": cache_start,
            "quantity": args.quantity, "capital": args.capital,
            "spread_rt": args.spread_rt, "cost_levels": cost_levels,
            "param1_start": args.param1_start, "param1_stop": args.param1_stop,
            "param1_step": args.param1_step, "grid_size": int(grid.shape[0]),
            "runs": len(rows), "bars": len(ohlcv), "db": db_path,
            "elapsed_sec": round(elapsed, 2),
        },
        "benchmark": _jsonable(bench),
        "rows": [_jsonable(r) for r in rows],
    }
    json_path = out_dir / f"{base}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    headers = list(bench.keys())
    csv_path = out_dir / f"{base}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in [bench, *rows]:
            writer.writerow([_csv_cell(r[k]) for k in headers])
    if args.xlsx:
        sheet_rows = sorted(rows, key=_sharpe_sort_key, reverse=True) + [bench]
        meta_lines = [f"symbol={args.symbol}", f"strategy={args.strategy}",
                      f"direction_mode={args.direction}", f"label={args.label}",
                      f"window={args.start}→{args.end}",
                      f"cost_levels={cost_levels}",
                      f"selection=全部行按 sharpe_ratio 降序（None 最后），末行为 buy-hold 基准"]
        append_sheet(args.xlsx, f"{args.direction}_{args.label or 'sweep'}",
                     headers,
                     [[_jsonable(r)[k] for k in headers] for r in sheet_rows],
                     meta_lines)
        print(f"Excel 追加 sheet '{args.direction}_{args.label or 'sweep'}' → {args.xlsx}")
    print(f"完成 {len(rows)} run（耗时 {elapsed:.1f}s）→ {json_path}")
    print(f"CSV → {csv_path}")
    return 0
