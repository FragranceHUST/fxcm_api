"""回测 CLI：python -m quant.cli backtest --db data/candles.db --symbol XAU/USD --years 3 ...

策略文件经 --strategy 传入（默认 strategies/vol_reversal.py，不入库），
模块需暴露 build_strategy(params, **kwargs) 工厂。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

from quant.cost import CostModel
from quant.data import DataFeed


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant.cli", description="量化回测（M1 引擎基座）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest", help="单参数集回测")
    bt.add_argument("--db", default="data/candles.db")
    bt.add_argument("--strategy", default="strategies/vol_reversal.py")
    bt.add_argument("--symbol", default="XAU/USD")
    bt.add_argument("--years", type=float, default=3.0)
    bt.add_argument("--direction", choices=["long", "short", "both"], default="long")
    bt.add_argument("--lots", type=int, default=1)
    bt.add_argument("--spread-rt", type=float, default=0.35, help="往返点差（价格单位）")
    bt.add_argument("--param", action="append", default=[], help="策略参数 k=v，可重复")
    bt.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args(argv)

    build = load_strategy_class(args.strategy)
    params = parse_params(args.param)
    strategy = build(params=params, symbol=args.symbol,
                     direction_mode=args.direction,
                     per_trade_lots=args.lots,
                     cost_model=CostModel(spread_rt=args.spread_rt))

    end_ts = int(time.time())
    start_ts = end_ts - int(args.years * 365 * 86400)
    feed = DataFeed(args.db)
    t0 = time.perf_counter()
    result = strategy.run_backtest(feed, start_ts, end_ts)
    elapsed = time.perf_counter() - t0
    feed.close()

    s = result.stats
    print(f"=== {args.symbol} {args.years}年 · {args.direction} · params={params} ===")
    print(f"交易数 {s['closed_trade_cnt']} | 胜率 {s['winrate']*100:.1f}% | "
          f"总PnL ${s['total_pnl']:.2f} | 夏普 {s['sharpe_ratio']:.2f} | "
          f"最大回撤 ${s['max_drawdown']:.2f} | 盈亏比 {s['max_profitloss_ratio']}")
    print(f"耗时 {elapsed:.2f}s（{len(result.trades)} 笔）")
    if args.out:
        strategy.output(result, args.out)
        print(f"结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
