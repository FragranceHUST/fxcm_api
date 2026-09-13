"""参数扫描：param1 × [param2] × 成本压力网格 + buy-hold 基准对照，结果落 JSON/CSV/Excel/热力图。

行情每个 worker 只查一次（含预热窗，H4 桶对齐），经 PreloadedFeed 在全部 run 间复用；
二维网格 run 在 spawn 进程池并行（--workers，1=串行确定性路径）。指标复用
compute_stats 口径，另算盈亏因子、平均持仓小时与按出场年份的已实现 PnL。
"""
from __future__ import annotations

import csv
import json
import math
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from quant.cli import load_strategy_class, parse_iso_date, parse_params, resolve_db
from quant.cost import CostModel
from quant.data import BacktestFeed, DataFeed, PreloadedFeed, h4_aligned_start
from quant.report import append_matrix_sheet, append_sheet
from quant.strategy import StrategyBase, quote_unit_value
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


def _row(param1: float | None, param2: float | None, cost_mult: float | None, stats: dict,
         trades: list[Trade], capital: float, label: str = "sweep",
         years: float | None = None) -> dict:
    return {
        "label": label,
        "param1": param1,
        "param2": param2,
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


def _benchmark_pnl_by_year(w, quantity: int, symbol: str) -> dict[str, float]:
    """buy-hold 按年归因用逐日盯市：各年 PnL = 年末浮动 PnL − 上年末浮动 PnL（含退出年）。"""
    entry = float(w.open[0])
    uv = quote_unit_value(symbol, w.close)          # JPY 报价 1/close，USD 报价恒 1（勿硬编码）
    fl = (w.close - entry) * quantity * uv
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
    uv = float(quote_unit_value(symbol, exit_px))   # USD 报价=1，JPY 报价=1/exit（勿硬编码 1/exit）
    profit = (exit_px - entry_px - spread_rt) * uv * quantity
    trade = Trade(trade_id=1, instrument=symbol, direction=Direction.ASK,
                  entry_price=entry_px, exit_price=exit_px, quantity=quantity,
                  entry_time=int(w.ts[0]), exit_time=int(w.ts[-1]),
                  profit=profit, closed=True, exit_reason="eod")
    row = _row(None, None, None, StrategyBase.calc_cost_function([trade]), [trade],
               capital, label="benchmark_buy_hold", years=years)
    row["pnl_by_year"] = _benchmark_pnl_by_year(w, quantity, symbol)   # MTM 按年归因，覆盖平仓年归因
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
        p2 = f" p2={r['param2']:.2f}" if r.get("param2") is not None else ""
        print(f"  {rank}. param1={r['param1']:.2f}{p2} trades={r['trades']} "
              f"winrate={r['winrate'] * 100:.1f}% pnl=${r['total_pnl']:.2f} "
              f"ret%={r['return_pct']:.2f} sharpe={r['sharpe_ratio']:.2f} "
              f"maxDD=${r['max_drawdown']:.2f}")


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return "" if not math.isfinite(v) else f"{v:.6f}"
    return str(v)


# ---------- 网格与并行执行 ----------

def result_base_name(symbol: str, start: str, end: str, kind: str, label: str) -> str:
    """结果文件基名（含 label，防不同变体互相覆盖）。"""
    tag = symbol.replace("/", "_")
    return f"{tag}_{start}_{end}_{kind}_{label or 'default'}"


def build_grid(start: float, stop: float, step: float) -> np.ndarray:
    """闭区间等差网格（浮点步长容差：终点 +step/2，再统一取整到 1e-10）。"""
    if step <= 0:
        raise SystemExit("网格步长必须为正")
    return np.round(np.arange(start, stop + step / 2, step), 10)


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


_WORKER: dict[str, Any] = {}


def init_backtest_worker(init: dict[str, Any]) -> None:
    """spawn worker 初始化：预加载行情 + 策略工厂（每进程一次）。"""
    feed = DataFeed(init["db_path"])
    ohlcv = feed.load(init["symbol"], 60, init["cache_start_ts"], init["end_ts"])
    feed.close()
    _WORKER.clear()
    _WORKER.update(init)
    _WORKER["feed"] = PreloadedFeed(ohlcv)
    _WORKER["build"] = load_strategy_class(init["strategy_path"])


def _strategy_params(p1: float, p2: float | None) -> dict[str, Any]:
    params: dict[str, Any] = {"param1": float(p1)}
    params.update(_WORKER.get("extra_params") or {})
    name = _WORKER.get("param2_name")
    if name and p2 is not None:
        params[str(name)] = float(p2)
    return params


def sweep_worker(task: tuple[float, float | None, float]) -> dict:
    """(p1, p2, cost_mult) → 指标行（盈亏因子/按年归因在 worker 内算，避免跨进程传 Trade 列表）。"""
    p1, p2, mult = task
    strategy = _WORKER["build"](params=_strategy_params(p1, p2), symbol=_WORKER["symbol"],
                                direction_mode=_WORKER["direction"],
                                quantity=_WORKER["quantity"],
                                total_capital=_WORKER["capital"],
                                cost_model=CostModel(spread_rt=_WORKER["spread_rt"] * mult))
    result = strategy.run_backtest(_WORKER["feed"], _WORKER["start_ts"], _WORKER["end_ts"])
    return _row(p1, p2, mult, result.stats, result.trades, _WORKER["capital"],
                years=_WORKER["years"])


def wfa_worker(task: tuple[int, float, float | None, int, int, float]
               ) -> tuple[int, float, float | None, dict]:
    """(fold_id, p1, p2, train_start, train_end, cost_mult) → 折内 train 段统计（不含 Trade 列表）。"""
    fold_id, p1, p2, train_start, train_end, mult = task
    strategy = _WORKER["build"](params=_strategy_params(p1, p2), symbol=_WORKER["symbol"],
                                direction_mode=_WORKER["direction"],
                                quantity=_WORKER["quantity"],
                                total_capital=_WORKER["capital"],
                                cost_model=CostModel(spread_rt=_WORKER["spread_rt"] * mult))
    result = strategy.run_backtest(_WORKER["feed"], train_start, train_end)
    return fold_id, p1, p2, result.stats


def run_backtest_tasks(tasks: list, init: dict[str, Any], worker: Callable,
                       workers: int, progress_every: int = 0) -> list:
    """并行执行回测任务；workers<=1 走串行确定性路径（测试用）。spawn 池每 worker init 一次。"""
    if workers <= 1:
        init_backtest_worker(init)
        return [worker(t) for t in tasks]
    ctx = multiprocessing.get_context("spawn")
    chunk = max(1, len(tasks) // (workers * 20))
    results: list = []
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                             initializer=init_backtest_worker, initargs=(init,)) as ex:
        for i, r in enumerate(ex.map(worker, tasks, chunksize=chunk), 1):
            results.append(r)
            if progress_every and i % progress_every == 0:
                print(f"  进度 {i}/{len(tasks)}")
    return results


def _pivot(rows: list[dict], p1s: np.ndarray, p2s: list[float], key: str) -> np.ndarray:
    """行=param1、列=param2 的指标矩阵；缺失格 NaN。"""
    mat = np.full((len(p1s), len(p2s)), np.nan)
    i1 = {float(p): i for i, p in enumerate(p1s)}
    i2 = {p: j for j, p in enumerate(p2s)}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        mat[i1[float(r["param1"])], i2[r["param2"]]] = float(v)
    return mat


def write_heatmap(png_path: Path, p1s: np.ndarray, p2s: list[float],
                  matrix: np.ndarray, title: str) -> None:
    """sharpe 热力图 PNG（param1 横轴、param2 纵轴，星标=矩阵最大值）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dx = float(p1s[1] - p1s[0]) if len(p1s) > 1 else 1.0
    dy = float(p2s[1] - p2s[0]) if len(p2s) > 1 else 1.0
    extent = (float(p1s[0]) - dx / 2, float(p1s[-1]) + dx / 2,
              float(p2s[0]) - dy / 2, float(p2s[-1]) + dy / 2)
    fig, ax = plt.subplots(figsize=(12, 7), dpi=110)
    im = ax.imshow(matrix.T, origin="lower", aspect="auto", cmap="RdYlGn",
                   extent=extent, interpolation="nearest")
    fig.colorbar(im, ax=ax, label="sharpe")
    finite = np.isfinite(matrix)
    if finite.any():
        i, j = np.unravel_index(np.argmax(np.where(finite, matrix, -np.inf)), matrix.shape)
        ax.scatter([float(p1s[i])], [float(p2s[j])], marker="*", s=140, color="blue",
                   zorder=3, label=f"max=({p1s[i]:.2f}, {p2s[j]:.2f})")
        ax.legend(loc="upper left", fontsize=8)
    ax.set_xlabel("param1")
    ax.set_ylabel("param2")
    ax.set_title(title)
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)


# ---------- sweep 命令 ----------

def cmd_sweep(args) -> int:
    load_strategy_class(args.strategy)             # 尽早失败：策略模块必须可加载
    if args.param2_name and None in (args.param2_start, args.param2_stop, args.param2_step):
        raise SystemExit("--param2-name 需要同时给出 --param2-start/stop/step")
    extra_params = parse_params(args.sparam)
    if args.param2_name and args.param2_name in extra_params:
        raise SystemExit(f"--sparam 与 --param2-name 冲突：{args.param2_name}")
    start_ts = parse_iso_date(args.start)
    end_ts = parse_iso_date(args.end) - 1          # end 排他：截至前一日最后一根 m1
    warmup_days = args.warmup_days                 # 须与策略默认预热一致（20 天）
    cache_start = h4_aligned_start(start_ts, warmup_days)

    db_path, source = resolve_db(args.symbol, args.db)
    print(f"数据源: {db_path}（{source}）")
    feed = DataFeed(db_path)
    ohlcv = feed.load(args.symbol, 60, cache_start, end_ts)
    feed.close()
    if len(ohlcv) == 0:
        raise SystemExit("预加载窗口内无数据，检查数据源与时间范围")
    span = (f"{datetime.fromtimestamp(int(ohlcv.ts[0]), tz=timezone.utc):%Y-%m-%d %H:%M}"
            f" → {datetime.fromtimestamp(int(ohlcv.ts[-1]), tz=timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"预加载 {len(ohlcv)} 根 m1（{span}），全部 run 复用")

    grid1 = build_grid(args.param1_start, args.param1_stop, args.param1_step)
    grid2: list[float | None] = ([float(p) for p in build_grid(
        args.param2_start, args.param2_stop, args.param2_step)]
        if args.param2_name else [None])
    cost_levels = [float(x) for x in str(args.cost_levels).split(",") if x.strip()]
    years = (end_ts - start_ts) / 86400 / 365.25
    workers = args.workers if args.workers > 0 else default_workers()

    tasks = [(float(p1), float(p2) if p2 is not None else None, mult)
             for mult in cost_levels for p1 in grid1 for p2 in grid2]
    init = {"db_path": db_path, "strategy_path": args.strategy, "symbol": args.symbol,
            "direction": args.direction, "quantity": args.quantity, "capital": args.capital,
            "spread_rt": args.spread_rt, "cache_start_ts": cache_start,
            "start_ts": start_ts, "end_ts": end_ts, "param2_name": args.param2_name,
            "years": years, "extra_params": extra_params}
    print(f"网格 {len(grid1)}×{len(grid2)} × 成本{cost_levels} = {len(tasks)} run，workers={workers}")
    t0 = time.perf_counter()
    rows: list[dict] = run_backtest_tasks(tasks, init, sweep_worker, workers,
                                          progress_every=max(1, len(tasks) // 50))
    elapsed = time.perf_counter() - t0
    for mult in cost_levels:
        print(f"=== 成本 ×{mult:g}（spread_rt={args.spread_rt * mult:g}）Top5 ===")
        _print_top(rows, mult)

    preloaded = PreloadedFeed(ohlcv)
    bench = _benchmark_row(preloaded, args.symbol, start_ts, end_ts,
                           args.spread_rt, args.quantity, args.capital, years=years)
    _print_benchmark(bench)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_tag = args.symbol.replace("/", "_")
    base = result_base_name(args.symbol, args.start, args.end, "sweep", args.label)
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
            "param1_step": args.param1_step, "grid1_size": int(grid1.shape[0]),
            "param2_name": args.param2_name,
            "param2_start": args.param2_start, "param2_stop": args.param2_stop,
            "param2_step": args.param2_step,
            "grid2_size": len(grid2) if args.param2_name else None,
            "workers": workers,
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
        _write_sweep_xlsx(args, rows, bench, grid1, grid2, headers, cost_levels)
    print(f"完成 {len(rows)} run（耗时 {elapsed:.1f}s）→ {json_path}")
    print(f"CSV → {csv_path}")
    return 0


def _write_sweep_xlsx(args, rows: list[dict], bench: dict, grid1: np.ndarray,
                      grid2: list[float | None], headers: list[str],
                      cost_levels: list[float]) -> None:
    """Excel：长表（sharpe 降序 + 基准行）；二维时每成本档追加 sharpe/pnl 矩阵表 + 热力图 PNG。"""
    sheet_rows = sorted(rows, key=_sharpe_sort_key, reverse=True) + [bench]
    meta_lines = [f"symbol={args.symbol}", f"strategy={args.strategy}",
                  f"direction_mode={args.direction}", f"label={args.label}",
                  f"window={args.start}→{args.end}",
                  f"cost_levels={cost_levels}",
                  f"param2={args.param2_name}∈[{args.param2_start},{args.param2_stop}]"
                  if args.param2_name else "param2=None（单维）",
                  f"selection=全部行按 sharpe_ratio 降序（None 最后），末行为 buy-hold 基准"]
    append_sheet(args.xlsx, f"{args.direction}_{args.label or 'sweep'}",
                 headers,
                 [[_jsonable(r)[k] for k in headers] for r in sheet_rows],
                 meta_lines)
    print(f"Excel 追加 sheet '{args.direction}_{args.label or 'sweep'}' → {args.xlsx}")
    if not args.param2_name:
        return
    out_dir = Path(args.out)
    p2_vals = [float(p) for p in grid2 if p is not None]
    for mult in cost_levels:
        sub = [r for r in rows if r["cost_mult"] == mult]
        for key, tag in (("sharpe_ratio", "sharpe"), ("total_pnl", "pnl")):
            mat = _pivot(sub, grid1, p2_vals, key)
            title = f"{args.direction}_{args.label or 'sweep'}_m{mult:g}_{tag}"[:31]
            append_matrix_sheet(args.xlsx, title, "param1",
                                [float(p) for p in grid1], p2_vals, mat,
                                meta_lines=[f"cost_mult={mult:g}", f"metric={key}"])
        png = out_dir / "heatmaps" / (
            f"{args.symbol.replace('/', '_')}_{args.direction}_{args.label or 'sweep'}"
            f"_m{mult:g}_sharpe.png")
        try:
            write_heatmap(png, grid1, p2_vals,
                          _pivot(sub, grid1, p2_vals, "sharpe_ratio"),
                          f"{args.symbol} {args.direction} cost x{mult:g} sharpe")
            print(f"热力图 → {png}")
        except ImportError:
            print("（matplotlib 不可用，跳过热力图）")
            return


def _print_benchmark(bench: dict) -> None:
    print(f"  benchmark_buy_hold: trades={bench['trades']} "
          f"winrate={bench['winrate'] * 100:.1f}% pnl=${bench['total_pnl']:.2f} "
          f"ret%={bench['return_pct']:.2f} sharpe={bench['sharpe_ratio']:.2f} "
          f"maxDD=${bench['max_drawdown']:.2f}")
