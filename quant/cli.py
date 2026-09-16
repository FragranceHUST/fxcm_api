"""回测 CLI：python -m quant.cli backtest --symbol XAU/USD --years 3 ...

数据源路由：--db 显式指定优先；否则官方库（candles_official.db）有该品种 m1
数据即用官方库，否则回退主库（candles.db）。
策略文件经 --strategy 传入（默认 strategies/signal_a.py，不入库），
模块需暴露 build_strategy(params, **kwargs) 工厂。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from quant.cost import CostModel
from quant.data import DataFeed

OFFICIAL_DB = "data/candles_official.db"
MAIN_DB = "data/candles.db"


def parse_iso_date(s: str) -> int:
    """ISO 日期/时刻（YYYY-MM-DD[THH:MM[:SS]]）→ UTC epoch 秒；纯日期按 UTC 00:00。"""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def resolve_db(symbol: str, db: str | None,
               official: str = OFFICIAL_DB, main: str = MAIN_DB) -> tuple[str, str]:
    """回测数据源路由，返回 (路径, 来源说明)。"""
    if db:
        return db, "显式指定"
    if Path(official).exists():
        try:
            conn = sqlite3.connect(f"file:{official}?mode=ro", uri=True)
            hit = conn.execute(
                "SELECT 1 FROM candles WHERE symbol=? AND tf=60 LIMIT 1", (symbol,)
            ).fetchone()
            conn.close()
            if hit:
                return official, "官方数据源"
        except sqlite3.Error:
            pass
    return main, "主库"


def load_strategy_class(path: str):
    spec = importlib.util.spec_from_file_location("user_strategy", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载策略模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["user_strategy"] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "build_strategy"):
        raise SystemExit(f"策略模块缺少 build_strategy 工厂: {path}")
    return mod.build_strategy


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        try:
            out[k] = float(v) if "." in v or "e" in v.lower() else int(v)
        except ValueError:
            out[k] = {"true": True, "false": False}.get(v.lower(), v)
    return out


def cmd_backtest(args) -> int:
    build = load_strategy_class(args.strategy)
    params = parse_params(args.param)
    strategy = build(params=params, symbol=args.symbol,
                     direction_mode=args.direction,
                     quantity=args.quantity,
                     cost_model=CostModel(spread_rt=args.spread_rt))

    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("--start 与 --end 必须同时给出")
        start_ts = parse_iso_date(args.start)
        end_ts = parse_iso_date(args.end) - 1      # end 排他：截至前一日最后一根 m1
        window = f"{args.start}→{args.end}"
    else:
        end_ts = int(time.time())
        start_ts = end_ts - int(args.years * 365 * 86400)
        window = f"{args.years}年"
    db_path, source = resolve_db(args.symbol, args.db)
    print(f"数据源: {db_path}（{source}）")
    feed = DataFeed(db_path)
    t0 = time.perf_counter()
    result = strategy.run_backtest(feed, start_ts, end_ts)
    elapsed = time.perf_counter() - t0
    feed.close()

    s = result.stats
    print(f"=== {args.symbol} {window} · {args.direction} · params={params} ===")
    print(f"交易数 {s['closed_trade_cnt']} | 胜率 {s['winrate']*100:.1f}% | "
          f"总PnL ${s['total_pnl']:.2f} | 夏普 {s['sharpe_ratio']:.2f} | "
          f"最大回撤 ${s['max_drawdown']:.2f} | 盈亏比 {s['max_profitloss_ratio']}")
    print(f"耗时 {elapsed:.2f}s（{len(result.trades)} 笔）")
    if args.out:
        strategy.output(result, args.out)
        print(f"结果已写入 {args.out}")
    return 0


def cmd_verify(args) -> int:
    """M0 数据完备性审计：全品种 × 全周期缺口检测 + 覆盖率。"""
    from datetime import datetime, timezone

    from quant.data_audit import SYMBOLS, TFS, audit_symbol_tf

    feed = DataFeed(args.db)
    report = []
    for symbol in SYMBOLS:
        for tf_name, tf_sec in TFS.items():
            r = audit_symbol_tf(feed, symbol, tf_sec)
            r["tf"] = tf_name
            report.append(r)
    feed.close()

    for r in report:
        if r["status"] == "EMPTY":
            print(f"{r['symbol']:8s} {r['tf']:4s} EMPTY")
            continue
        rng = (f"{datetime.fromtimestamp(r['earliest'], tz=timezone.utc):%Y-%m-%d}"
               f" ~ {datetime.fromtimestamp(r['latest'], tz=timezone.utc):%Y-%m-%d}")
        print(f"{r['symbol']:8s} {r['tf']:4s} {r['count']:>7} 根 {rng} "
              f"覆盖≈{r['coverage']*100:5.1f}% {r['status']}")
        for g0, g1, _ in r["gaps"]:
            print(f"          ↳ 缺口 {datetime.fromtimestamp(g0, tz=timezone.utc):%m-%d %H:%M}"
                  f" → {datetime.fromtimestamp(g1, tz=timezone.utc):%m-%d %H:%M}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"报告已写入 {args.out}")
    return 1 if any(r["status"].startswith("WARN") for r in report) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant.cli", description="量化回测引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest", help="单参数集回测")
    bt.add_argument("--db", default=None,
                    help="数据源库（默认自动路由：官方库优先，无该品种回退主库）")
    bt.add_argument("--strategy", default="strategies/signal_a.py")
    bt.add_argument("--symbol", default="XAU/USD")
    bt.add_argument("--years", type=float, default=3.0)
    bt.add_argument("--start", default=None,
                    help="起始 ISO 日期（YYYY-MM-DD，UTC）；给出时与 --end 一起覆盖 --years")
    bt.add_argument("--end", default=None,
                    help="结束 ISO 日期（UTC，排他：数据截至前一日最后一根 m1）")
    bt.add_argument("--direction", choices=["long", "short", "both"], default="long")
    bt.add_argument("--quantity", type=int, default=1,
                    help="数量，单位=品种原生数量单位（XAU/USD 为盎司，非手）")
    bt.add_argument("--spread-rt", type=float, default=0.35, help="往返点差（价格单位）")
    bt.add_argument("--param", action="append", default=[], help="策略参数 k=v，可重复")
    bt.add_argument("--out", default=None, help="结果 JSON 输出路径")

    vf = sub.add_parser("verify", help="M0 数据完备性审计（缺口+覆盖率）")
    vf.add_argument("--db", default="data/candles.db")
    vf.add_argument("--out", default=None, help="审计报告 JSON 输出路径")

    sw = sub.add_parser("sweep", help="param1 × 成本压力网格扫描 + buy-hold 基准")
    sw.add_argument("--db", default=None,
                    help="数据源库（默认自动路由：官方库优先，无该品种回退主库）")
    sw.add_argument("--strategy", default="strategies/vol_reversal.py")
    sw.add_argument("--symbol", default="USD/JPY")
    sw.add_argument("--start", default="2020-01-01", help="窗口起始 ISO 日期（UTC）")
    sw.add_argument("--end", default="2025-01-01", help="窗口结束 ISO 日期（UTC，排他）")
    sw.add_argument("--param1-start", type=float, default=0.0)
    sw.add_argument("--param1-stop", type=float, default=1.0)
    sw.add_argument("--param1-step", type=float, default=0.02)
    sw.add_argument("--param2-name", default=None,
                    help="第二维参数名（如 tp_atr_mult=TP 随信号桶 ATR 的倍数）；缺省=单维扫参")
    sw.add_argument("--param2-start", type=float, default=None)
    sw.add_argument("--param2-stop", type=float, default=None)
    sw.add_argument("--param2-step", type=float, default=None)
    sw.add_argument("--workers", type=int, default=0,
                    help="并行 worker 进程数（0=自动 cpu-1，1=串行确定性路径）")
    sw.add_argument("--warmup-days", type=int, default=20,
                    help="预热天数（须与策略默认一致，仅用于预加载窗口）")
    sw.add_argument("--quantity", type=int, default=50000)
    sw.add_argument("--capital", type=float, default=5000.0)
    sw.add_argument("--spread-rt", type=float, default=0.01, help="基准往返点差（价格单位）")
    sw.add_argument("--cost-levels", default="1,2,3", help="成本压力倍数，逗号分隔")
    sw.add_argument("--direction", choices=["long", "short", "both"], default="long")
    sw.add_argument("--sparam", action="append", default=[],
                    help="策略额外参数 k=v（并入每个 run 的 params，如 sl_pips=35），可重复")
    sw.add_argument("--label", default="", help="结果标签（进入 meta 与 xlsx sheet 名）")
    sw.add_argument("--xlsx", default=None, help="Excel 工作簿路径（追加式 sheet，不覆盖）")
    sw.add_argument("--out", default="data/quant_results", help="结果输出目录")

    wf = sub.add_parser("wfa", help="Walk-Forward Analysis：折内选参 + OOS 拼接")
    wf.add_argument("--db", default=None,
                    help="数据源库（默认自动路由：官方库优先，无该品种回退主库）")
    wf.add_argument("--strategy", default="strategies/vol_reversal.py")
    wf.add_argument("--symbol", default="USD/JPY")
    wf.add_argument("--start", default="2020-01-01", help="窗口起始 ISO 日期（UTC）")
    wf.add_argument("--end", default="2025-01-01", help="窗口结束 ISO 日期（UTC，排他）")
    wf.add_argument("--direction", choices=["long", "short", "both"], default="long")
    wf.add_argument("--sparam", action="append", default=[],
                    help="策略额外参数 k=v（并入每个 run 的 params，如 sl_pips=35），可重复")
    wf.add_argument("--quantity", type=int, default=50000)
    wf.add_argument("--capital", type=float, default=5000.0)
    wf.add_argument("--spread-rt", type=float, default=0.01, help="往返点差（价格单位）")
    wf.add_argument("--cost-levels", default="1",
                    help="成本压力倍数（取第一个；WFA 每次运行单成本档）")
    wf.add_argument("--train-months", type=int, default=24, help="训练窗长度（日历月）")
    wf.add_argument("--test-months", type=int, default=6, help="测试窗长度与步进（日历月）")
    wf.add_argument("--param1-start", type=float, default=0.0)
    wf.add_argument("--param1-stop", type=float, default=1.0)
    wf.add_argument("--param1-step", type=float, default=0.02)
    wf.add_argument("--param2-name", default=None,
                    help="第二维参数名（如 tp_atr_mult）；缺省=单维选参")
    wf.add_argument("--param2-start", type=float, default=None)
    wf.add_argument("--param2-stop", type=float, default=None)
    wf.add_argument("--param2-step", type=float, default=None)
    wf.add_argument("--param3-name", default=None,
                    help="第三维参数名（如 sl_atr_mult）；缺省=不启用。"
                         "选参：逐切片跑 5×5 邻域规则，跨切片取邻域得分最高（平票按 p3 升序）")
    wf.add_argument("--param3-start", type=float, default=None)
    wf.add_argument("--param3-stop", type=float, default=None)
    wf.add_argument("--param3-step", type=float, default=None)
    wf.add_argument("--workers", type=int, default=0,
                    help="并行 worker 进程数（0=自动 cpu-1，1=串行确定性路径）")
    wf.add_argument("--min-train-trades", type=int, default=30,
                    help="折内选参的最低 train 平仓笔数（不足则该折跳过 OOS）")
    wf.add_argument("--oos-topk", type=int, default=1,
                    help="每折取 IS 邻域前 K 组参数分别跑 OOS 并等权拼接（1=仅取高原中心，默认）")
    wf.add_argument("--warmup-days", type=int, default=20,
                    help="预热天数（须与策略默认一致，仅用于预加载窗口）")
    wf.add_argument("--out", default="data/quant_results", help="结果输出目录")
    wf.add_argument("--label", default="", help="结果标签（进入 meta、聚合 label 与 xlsx sheet 名）")
    wf.add_argument("--xlsx", default=None, help="Excel 工作簿路径（追加式 sheet，不覆盖）")

    args = ap.parse_args(argv)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "sweep":
        from quant.optimize import cmd_sweep   # 延迟导入：optimize 反向引用本模块
        return cmd_sweep(args)
    if args.cmd == "wfa":
        from quant.validate import cmd_wfa     # 延迟导入：validate 反向引用本模块
        return cmd_wfa(args)
    return cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
