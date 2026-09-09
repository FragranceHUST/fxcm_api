"""CLI 入口：check / describe / login / stream / guard / positions / open / close / sl。

用法：
  .venv/bin/python -m fxcm_api.main check
  .venv/bin/python -m fxcm_api.main describe --config config.demo.json
  .venv/bin/python -m fxcm_api.main login   --config config.demo.json
  .venv/bin/python -m fxcm_api.main stream  --config config.demo.json --symbol EUR/USD --seconds 10
  .venv/bin/python -m fxcm_api.main guard   --config config.demo.json
  .venv/bin/python -m fxcm_api.main positions --config config.demo.json
  .venv/bin/python -m fxcm_api.main open --config config.demo.json --symbol EUR/USD --buy --amount 1000
  .venv/bin/python -m fxcm_api.main close --config config.demo.json --trade-id T123456
  .venv/bin/python -m fxcm_api.main sl --config config.demo.json --trade-id T123456 --pips 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from forexconnect import ForexConnect, TableListener
from fxcm_api.data import backfill as backfill_module
from fxcm_api.data.store import CandleStore

from fxcm_api.config import GuardSettings, load_config
from fxcm_api.pips import pip_size
from fxcm_api.session import connect, disconnect
from fxcm_api.stop_manager import StopManager
from fxcm_api.stops import TradeSnap
from fxcm_api.trading import (
    close_trade,
    find_offer,
    open_market,
    resolve_account,
    trade_is_buy,
    wait_for_new_trade,
    wait_trade_gone,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DAEMON_URL = "http://127.0.0.1:8911"


def daemon_alive() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def daemon_request(path: str, method: str = "GET", body: dict | None = None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{DAEMON_URL}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def confirm_real(env: str, action: str) -> bool:
    if env != "real":
        return True
    return input(f"⚠⚠ 真实资金{action}，输入 yes 确认: ").strip().lower() == "yes"


def proxy_positions(args: argparse.Namespace) -> int:
    rc, rows = daemon_request(f"/api/{args.env}/positions")
    if rc != 200:
        print(f"代理失败 [{rc}]"); return 1
    print(f"持仓数: {len(rows)} [{args.env}, 经 daemon 会话，未登录]")
    for r in rows:
        d = 2 if r["symbol"] == "XAU/USD" else 5
        stop = f"{r['stop']:.{d}f}" if r["stop"] else "无"
        sid = r["stop_order_id"] or "-"
        print(f"{r['trade_id']:<10} {r['symbol']:<9} "
              f"{'BUY' if r['is_buy'] else 'SELL':<5} {r['amount']:>6} "
              f"{r['open_rate']:>10.{d}f} {stop:>10} "
              f"{sid:<10} {r['gross_pl']:>9.2f}")
    return 0


def proxy_order(args: argparse.Namespace) -> int:
    if not confirm_real(args.env, "开仓"):
        print("已取消"); return 1
    body = {"order_type": "market", "symbol": args.symbol,
            "is_buy": args.buy, "amount": args.amount}
    if args.env == "real":
        body["confirm"] = True
    rc, r = daemon_request(f"/api/{args.env}/orders", "POST", body)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if rc == 200 else 1


def proxy_close(args: argparse.Namespace) -> int:
    if not confirm_real(args.env, "平仓"):
        print("已取消"); return 1
    body = {"confirm": True} if args.env == "real" else {}
    if args.amount:
        body["amount"] = args.amount
    rc, r = daemon_request(f"/api/{args.env}/positions/{args.trade_id}/close", "POST", body)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if rc == 200 else 1


def proxy_sl(args: argparse.Namespace) -> int:
    if args.price is None:
        if args.pips is None:
            print("需要 --price 或 --pips"); return 2
        rc0, rows = daemon_request(f"/api/{args.env}/positions")
        trade = next((t for t in rows if str(t["trade_id"]) == str(args.trade_id)), None)
        if not trade:
            print("持仓不存在"); return 1
        pip = 0.1 if trade["symbol"] == "XAU/USD" else \
            (0.01 if trade["symbol"] == "USD/JPY" else 0.0001)
        args.price = trade["open_rate"] - args.pips * pip if trade["is_buy"] \
            else trade["open_rate"] + args.pips * pip
        print(f"换算止损价: {args.price}")
    if not confirm_real(args.env, "改损"):
        print("已取消"); return 1
    body = {"price": args.price}
    if args.env == "real":
        body["confirm"] = True
    rc, r = daemon_request(f"/api/{args.env}/positions/{args.trade_id}/sl", "PATCH", body)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if rc == 200 else 1


def proxy_history(args: argparse.Namespace) -> int:
    rc, rows = daemon_request(f"/api/{args.env}/history/trades?limit={args.limit}")
    if rc != 200:
        print(f"代理失败 [{rc}]"); return 1
    print(f"已平仓 {len(rows)} 笔 [{args.env}]")
    for t in rows:
        d = 2 if t["symbol"] == "XAU/USD" else 5
        ct = str(t["close_time"])[:19]
        print(f"{t['trade_id']:<10} {t['symbol']:<9} "
              f"{'BUY' if t['is_buy'] else 'SELL':<5} {t['amount']:>6} "
              f"{t['open_rate']:>10.{d}f} {t['close_rate']:>10.{d}f} "
              f"{t['gross_pl']:>9.2f} {ct}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    import forexconnect
    print(f"forexconnect 包路径: {forexconnect.__file__}")
    try:
        from forexconnect import fxcorepy  # noqa: F401  触发编译扩展加载
        print("fxcorepy 编译扩展: OK")
    except OSError as exc:
        print(f"fxcorepy 加载失败: {exc}")
        print("请运行: bash scripts/patch_forexconnect_mac.sh")
        return 1
    try:
        import numpy, pandas  # noqa: F401
        print(f"numpy {numpy.__version__} / pandas {pandas.__version__}: OK")
    except ImportError as exc:
        print(f"numpy/pandas 缺失: {exc}")
        return 1
    print("环境自检通过")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    cred, _ = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        for table_type, label in [(ForexConnect.ACCOUNTS, "ACCOUNTS"),
                                  (ForexConnect.TRADES, "TRADES"),
                                  (ForexConnect.OFFERS, "OFFERS")]:
            table = fx.get_table(table_type)
            if table is None or table.size == 0:
                print(f"{label}: 空")
                continue
            row = table.get_row(0)
            print(f"{label} 行字段 ({table.size} 行):")
            print("  ", ", ".join(str(col.id) for col in row.columns))
        return 0
    finally:
        disconnect(fx)


def cmd_login(args: argparse.Namespace) -> int:
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        accounts = fx.get_table(ForexConnect.ACCOUNTS)
        print(f"账户数: {accounts.size if accounts else 0}")
        if accounts and accounts.size:
            row = accounts.get_row(0)
            print(f"  第一账户: {row.account_id} 余额={row.balance}")
        trades = fx.get_table(ForexConnect.TRADES)
        print(f"当前持仓数: {trades.size if trades else 0}")
        print("登录验证通过；guard 设置:", guard_summary(guard))
        return 0
    finally:
        disconnect(fx)


def cmd_stream(args: argparse.Namespace) -> int:
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        deadline = time.time() + args.seconds
        printed = 0
        while time.time() < deadline:
            for row in fx.get_table(ForexConnect.OFFERS):
                if args.symbol and args.symbol not in (row.instrument or ""):
                    continue
                digits = int(row.digits)
                print(f"{row.instrument:<10} bid={row.bid:.{digits}f} ask={row.ask:.{digits}f} "
                      f"point={row.point_size} digits={digits}")
                printed += 1
                if printed >= args.lines:
                    return 0
            time.sleep(max(guard.poll_interval_ms, 200) / 1000.0)
        return 0
    finally:
        disconnect(fx)


def cmd_guard(args: argparse.Namespace) -> int:
    if daemon_alive():
        print("guard 由 daemon 承担（双环境循环运行中），CLI guard 已停用以避免两个管家抢活")
        return 0
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        manager = StopManager(fx, guard)
        manager.run_forever()
        return 0
    except KeyboardInterrupt:
        print("\n收到中断，登出并退出")
        return 0
    finally:
        disconnect(fx)


def cmd_positions(args: argparse.Namespace) -> int:
    if daemon_alive():
        return proxy_positions(args)
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        offers = {row.offer_id: row for row in fx.get_table(ForexConnect.OFFERS)}
        trades = fx.get_table(ForexConnect.TRADES)
        count = trades.size if trades else 0
        print(f"持仓数: {count}")
        if count:
            print(f"{'trade_id':<10} {'品种':<9} {'方向':<5} {'手数':>6} "
                  f"{'开仓价':>10} {'当前止损':>10} {'止损单ID':<10} {'浮动盈亏':>9}")
            for row in trades:
                offer = offers.get(row.offer_id)
                sym = offer.instrument if offer else "?"
                digits = int(offer.digits) if offer else 5
                stop = f"{row.stop:.{digits}f}" if row.stop else "无"
                print(f"{row.trade_id:<10} {sym:<9} {'BUY' if trade_is_buy(row) else 'SELL':<5} "
                      f"{row.amount:>6} {row.open_rate:>10.{digits}f} {stop:>10} "
                      f"{(row.stop_order_id or '-'):<10} {row.gross_pl:>9.2f}")
        else:
            print("（用 open 命令开一笔测试仓位后，guard 即可接管）")
        return 0
    finally:
        disconnect(fx)


def cmd_open(args: argparse.Namespace) -> int:
    if daemon_alive():
        return proxy_order(args)
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        offer = find_offer(fx, args.symbol)
        account_id = resolve_account(fx, guard.account_id)
        digits = int(offer.digits)
        known = {row.trade_id for row in fx.get_table(ForexConnect.TRADES)
                 if row.offer_id == offer.offer_id}
        open_market(fx, account_id, offer.instrument, args.buy, args.amount)
        row = wait_for_new_trade(fx, offer.offer_id, known)
        if row is None:
            print("⚠️ 未捕获到新持仓——检查上方响应输出（可能手数不合法或被拒）")
            return 1
        stop_txt = f"{row.stop:.{digits}f}" if row.stop else "无(等待 guard INIT)"
        print(f"✅ 已开仓: trade_id={row.trade_id} {offer.instrument} "
              f"{'BUY' if trade_is_buy(row) else 'SELL'} amount={row.amount} "
              f"open_rate={row.open_rate:.{digits}f} stop={stop_txt}")
        print("提示：guard 会自动为该持仓补挂/推进止损（dry_run=true 时仅打印动作）")
        return 0
    finally:
        disconnect(fx)


def cmd_close(args: argparse.Namespace) -> int:
    cred, _ = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        account_id = resolve_account(fx)
        row = next((r for r in fx.get_table(ForexConnect.TRADES)
                    if r.trade_id == args.trade_id), None)
        if row is None:
            print(f"未找到持仓 {args.trade_id}（用 positions 查看现有持仓）")
            return 1
        amount = args.amount or int(row.amount)
        close_trade(fx, account_id, args.trade_id, amount)
        if wait_trade_gone(fx, args.trade_id):
            print(f"✅ 持仓 {args.trade_id} 已平仓")
            return 0
        print("⚠️ 平仓请求已发送，但 6 秒内持仓仍可见——检查 MESSAGES/响应")
        return 1
    finally:
        disconnect(fx)


def cmd_sl(args: argparse.Namespace) -> int:
    if daemon_alive():
        return proxy_sl(args)
    cred, guard = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        account_id = resolve_account(fx)
        trades = fx.get_table(ForexConnect.TRADES)
        row = next((r for r in trades if r.trade_id == args.trade_id), None)
        if row is None:
            print(f"未找到持仓 {args.trade_id}（用 positions 查看现有持仓）")
            return 1
        offers = {o.offer_id: o for o in fx.get_table(ForexConnect.OFFERS)}
        offer = offers[row.offer_id]
        pip = pip_size(offer.instrument, offer.point_size, int(offer.digits),
                       guard.pip_overrides)
        if args.price is not None:
            target = args.price
        else:
            step = args.pips * pip
            target = row.open_rate + step if trade_is_buy(row) else row.open_rate - step
        mgr = StopManager(fx, guard)
        mgr.account_id = account_id
        snap = TradeSnap(trade_id=row.trade_id, offer_id=row.offer_id,
                         symbol=offer.instrument, is_buy=trade_is_buy(row),
                         amount=int(row.amount), open_rate=row.open_rate,
                         stop_order_id=(row.stop_order_id or "") or None,
                         current_stop=float(row.stop) if row.stop else None)
        reason = f"MANUAL({'EDIT' if snap.stop_order_id else 'CREATE'})"
        mgr._apply(snap, round(target, int(offer.digits)), reason)
        return 0
    finally:
        disconnect(fx)


def cmd_measure(args: argparse.Namespace) -> int:
    """事件模式实测行情速率：一次登录，同时统计多个品种的 tick 到包率。"""
    cred, _ = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        offers_table = fx.get_table(ForexConnect.OFFERS)

        targets: dict[str, str] = {}     # offer_id -> symbol
        counters: dict[str, list] = {}   # offer_id -> [total, bid_changes, buckets, bucket_start, last_bid, total_start]
        for symbol in args.symbols:
            offer = find_offer(fx, symbol)
            targets[offer.offer_id] = offer.instrument
            counters[offer.offer_id] = [0, 0, [], time.time(), None, time.time()]

        def on_changed(_listener, _row_id, row):
            stat = counters.get(row.offer_id)
            if stat is None:
                return
            stat[0] += 1
            now = time.time()
            if not stat[2] or now - stat[3] >= args.bucket:
                stat[2].append(0)
                stat[3] = now
            stat[2][-1] += 1
            if row.bid != stat[4]:       # bid 价格真实变化的次数（去重）
                stat[4] = row.bid
                stat[1] += 1

        listener = TableListener(on_changed_callback=on_changed)
        listener.subscribe(offers_table)

        print(f"测量中：{', '.join(args.symbols)} 共 {args.seconds} 秒（每 {args.bucket}s 一个桶）...")
        time.sleep(args.seconds)
        listener.unsubscribe()

        print(f"\n{'品种':<10} {'更新事件':>8} {'平均/s':>8} {'峰值桶/s':>9} {'bid变化':>8}  桶明细")
        for offer_id, symbol in targets.items():
            total, bid_changes, buckets, _bucket_start, _last_bid, total_start = counters[offer_id]
            duration = time.time() - total_start
            avg = total / duration if duration > 0 else 0.0
            peak = (max(buckets) / args.bucket) if buckets else 0.0
            detail = " ".join(str(b) for b in buckets)
            print(f"{symbol:<10} {total:>8} {avg:>8.1f} {peak:>9.1f} {bid_changes:>8}  [{detail}]")
        print("\n说明：统计 OFFERS 表 UPDATE 事件（服务器每次推送计 1 次，"
              "单次事件可能同时变更 bid/ask 多字段）；bid变化 = 去重后的不同 bid 价格数。")
        return 0
    finally:
        disconnect(fx)


def cmd_probe_history(args: argparse.Namespace) -> int:
    """实测单品种单周期的容量与深度，输出 JSON。"""
    cred, _ = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        result = backfill_module.probe(fx, args.symbol, args.tf)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        disconnect(fx)


def cmd_backfill(args: argparse.Namespace) -> int:
    """历史K线批量回填（支持 Ctrl+C 中断后续传）。"""
    cred, _ = load_config(args.config)
    tf_labels = [t.strip() for t in args.tf.split(",") if t.strip()]
    for t in tf_labels:
        if t not in backfill_module.TF_SECONDS:
            print(f"未知周期: {t}（可选 {'/'.join(backfill_module.TF_SECONDS)}）")
            return 2
    store = CandleStore(Path(args.data_dir) / "candles.db")
    fx = None
    try:
        fx = connect(cred, retries=4)
        def progress(symbol, tf, total, requests, cursor_ts):
            dt = datetime.fromtimestamp(cursor_ts, tz=timezone.utc)
            print(f"[{symbol} {tf}] 已存 {total} 根 / {requests} 请求，"
                  f"回填至 {dt:%Y-%m-%d}", flush=True)
        for symbol in args.symbols:
            for tf in tf_labels:
                r = backfill_module.backfill(fx, symbol, tf, args.years, store,
                                             delay_ms=args.delay_ms, progress=progress)
                print(f"== {symbol} {tf} 完成: {r['bars']} 根 / {r['requests']} 请求")
        return 0
    except KeyboardInterrupt:
        print("中断：游标已保存，重跑同一命令即续传")
        return 130
    finally:
        disconnect(fx)
        store.close()


def cmd_history(args: argparse.Namespace) -> int:
    if daemon_alive():
        return proxy_history(args)
    cred, _ = load_config(args.config)
    fx = None
    try:
        fx = connect(cred)
        print(f"已平仓交易 [{args.config}]")
        offers = {r.offer_id: r.instrument for r in fx.get_table(ForexConnect.OFFERS)}
        closed = fx.get_table(ForexConnect.CLOSED_TRADES)
        rows = list(closed or [])[:args.limit]
        for t in rows:
            d = 2 if offers.get(t.offer_id) == "XAU/USD" else 5
            print(f"{t.trade_id:<10} {offers.get(t.offer_id, '?'):<9} "
                  f"{t.amount:>6} {t.open_rate:>10.{d}f} {t.close_rate:>10.{d}f} "
                  f"{t.gross_pl:>9.2f} {t.close_time}")
        return 0
    finally:
        disconnect(fx)


def guard_summary(guard: GuardSettings) -> str:
    return (f"初始SL={guard.initial_sl_pips}pips 保本触发={guard.be_trigger_pips}pips "
            f"缓冲={guard.be_buffer_pips}pips 移动止损={guard.use_trailing} "
            f"周期={guard.poll_interval_ms}ms dry_run={guard.dry_run} "
            f"品种过滤={guard.symbol_filter or '全账户'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fxcm_api", description="FXCM 止损管家")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="环境自检（不登录）")
    sub.add_parser("describe", help="登录后打印各表格行字段名")
    sub.add_parser("login", help="登录验证并打印账户摘要")

    p_stream = sub.add_parser("stream", help="打印行情流若干行后退出")
    p_stream.add_argument("--symbol", default="EUR/USD")
    p_stream.add_argument("--seconds", type=float, default=10.0)
    p_stream.add_argument("--lines", type=int, default=5)

    p_pos = sub.add_parser("positions", help="打印当前持仓快照（guard 视角字段）")
    p_pos.add_argument("--env", default="demo", choices=["demo", "real"])

    p_hist = sub.add_parser("history", help="已平仓交易历史（daemon 在线零登录）")
    p_hist.add_argument("--env", default="demo", choices=["demo", "real"])
    p_hist.add_argument("--limit", type=int, default=50)

    sub.add_parser("guard", help="运行动态止损管家（Ctrl+C 退出）")
    p_open = sub.add_parser("open", help="市价开一笔测试仓位（daemon 在线时走代理）")
    p_open.add_argument("--env", default="demo", choices=["demo", "real"])
    p_open.add_argument("--symbol", required=True, help="品种，如 EUR/USD")
    p_open.add_argument("--buy", action="store_true", help="买入（默认卖出）")
    p_open.add_argument("--amount", type=int, required=True, help="手数（base unit 的倍数，通常 1000 起）")

    p_close = sub.add_parser("close", help="平仓指定持仓（daemon 在线时走代理）")
    p_close.add_argument("--env", default="demo", choices=["demo", "real"])
    p_close.add_argument("--trade-id", required=True)
    p_close.add_argument("--amount", type=int, default=None, help="部分平仓手数（默认全平）")

    p_sl = sub.add_parser("sl", help="手动设置某持仓的止损（daemon 在线时走代理）")
    p_sl.add_argument("--env", default="demo", choices=["demo", "real"])
    p_sl.add_argument("--trade-id", required=True)
    p_sl.add_argument("--pips", type=float, default=None, help="相对开仓价的止损距离（多单向下/空单向上）")
    p_sl.add_argument("--price", type=float, default=None, help="绝对止损价（与 --pips 二选一）")

    p_measure = sub.add_parser("measure", help="实测行情速率（事件订阅计数）")
    p_measure.add_argument("--symbols", nargs="+", default=["EUR/USD", "XAU/USD"],
                           help="要测量的品种列表")
    p_measure.add_argument("--seconds", type=float, default=60.0)
    p_measure.add_argument("--bucket", type=float, default=10.0, help="分桶时长（秒）")

    p_probe = sub.add_parser("probe-history", help="实测历史数据容量与可回溯深度")
    p_probe.add_argument("--symbol", default="XAU/USD")
    p_probe.add_argument("--tf", default="m1", choices=["1m", "15m", "1h", "4h", "1d"])

    p_backfill = sub.add_parser("backfill", help="历史K线回填到 SQLite（断点续传）")
    p_backfill.add_argument("--symbols", nargs="+",
                            default=["XAU/USD", "USD/JPY", "EUR/USD"])
    p_backfill.add_argument("--tf", default="1m,15m,1h,4h,1d",
                            help="逗号分隔周期：1m,15m,1h,4h,1d")
    p_backfill.add_argument("--years", type=float, default=10.0)
    p_backfill.add_argument("--delay-ms", type=int, default=300)
    p_backfill.add_argument("--data-dir", default="data")

    return parser


COMMANDS = {
    "check": cmd_check,
    "describe": cmd_describe,
    "login": cmd_login,
    "stream": cmd_stream,
    "guard": cmd_guard,
    "positions": cmd_positions,
    "open": cmd_open,
    "close": cmd_close,
    "sl": cmd_sl,
    "measure": cmd_measure,
    "history": cmd_history,
    "probe-history": cmd_probe_history,
    "backfill": cmd_backfill,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

