#!/usr/bin/env python
"""从 FXCM 官方 candledata 服务下载 m1 周文件（csv.gz）并导入独立 SQLite 库。

数据源: https://candledata.fxcorporate.com/m1/{INSTRUMENT}/{YEAR}/{WEEK}.csv.gz
（WEEK 为 ISO 周序号 1..53；404 = 该周无数据，静默跳过。）

为什么用独立库（默认 data/candles_official.db，与主库 candles.db 同 schema）:
官方数据是 Active Trader 最小点差的指示性报价，与主库的账户实际报价流
（get_history / OFFERS 聚合）存在潜在基差；若混写同一主键会污染实盘口径序列。
官方数据只做补缺观察，先用 --compare 输出重叠区间基差分布，再决定是否值得混入。

用法（零新依赖，标准库 + CandleStore 复用；不登录 FXCM，纯 HTTP 下载）:
    .venv/bin/python scripts/backfill_official.py --symbols EURUSD --years 2026
    .venv/bin/python scripts/backfill_official.py --compare --symbols EURUSD
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import math
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxcm_api.data.store import CandleStore, FullBar

URL_TMPL = "https://candledata.fxcorporate.com/m1/{instrument}/{year}/{week}.csv.gz"
USER_AGENT = "fxcm_api-backfill/1.0"
TF_M1 = 60
CSV_HEADER = "DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose"
DT_FORMAT = "%m/%d/%Y %H:%M:%S.%f"
ISO_WEEKS = 53
RETRY_DELAYS = (1.0, 3.0)
DEFAULT_DB = "data/candles_official.db"
DEFAULT_MAIN_DB = "data/candles.db"

FetchFn = Callable[[str, int, int], str | None]


def map_symbol(official: str) -> str:
    s = official.strip().upper().replace("/", "")
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    raise ValueError(f"无法映射官方品种名: {official!r}（期望 6 字母，如 EURUSD）")


def parse_years(spec: str) -> list[int]:
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise ValueError(f"年份区间非法: {part!r}") from None
            if lo > hi:
                raise ValueError(f"年份区间起止颠倒: {part!r}")
            years.update(range(lo, hi + 1))
        else:
            try:
                years.add(int(part))
            except ValueError:
                raise ValueError(f"年份非法: {part!r}") from None
    if not years:
        raise ValueError(f"年份参数为空: {spec!r}")
    return sorted(years)


def parse_datetime_utc(text: str) -> int:
    try:
        dt = datetime.strptime(text.strip(), DT_FORMAT)
    except ValueError:
        raise ValueError(f"DateTime 非法（期望 {DT_FORMAT}, UTC）: {text!r}") from None
    ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
    if ts % 60:
        raise ValueError(f"非整分钟对齐: {text!r}")
    return ts


def parse_week_csv(text: str) -> list[FullBar]:
    reader = csv.reader(io.StringIO(text.replace("\r\n", "\n")))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("CSV 为空") from None
    if [h.strip() for h in header] != CSV_HEADER.split(","):
        raise ValueError(f"CSV 表头不符（期望 {CSV_HEADER}）: {header!r}")
    bars: list[FullBar] = []
    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != 9:
            raise ValueError(f"列数非法（期望 9）: {row!r}")
        try:
            ts = parse_datetime_utc(row[0])
            bo, bh, bl, bc, ao, ah, al, ac = (float(v) for v in row[1:9])
        except ValueError as e:
            raise ValueError(f"行解析失败: {row!r}: {e}") from None
        bars.append((ts, bo, bh, bl, bc, ao, ah, al, ac, 0))
    return bars


def http_fetch_week(instrument: str, year: int, week: int, timeout: float = 30.0) -> str | None:
    url = URL_TMPL.format(instrument=instrument, year=year, week=week)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if not raw:
        return None
    return gzip.decompress(raw).decode("utf-8", errors="replace")


def fetch_week_with_retry(instrument: str, year: int, week: int,
                          retry_delays: tuple[float, ...] = RETRY_DELAYS) -> str | None:
    last: BaseException | None = None
    for delay in (0.0, *retry_delays):
        if delay:
            time.sleep(delay)
        try:
            return http_fetch_week(instrument, year, week)
        except Exception as e:
            last = e
    raise RuntimeError(f"下载失败 {instrument} {year} W{week}") from last


def run_backfill(symbols: list[str], years: list[int], db_path: str | Path,
                 delay_ms: int = 300, fetch: FetchFn | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print) -> list[dict]:
    fetch = fetch or fetch_week_with_retry
    store = CandleStore(db_path)
    results: list[dict] = []
    first_request = True
    try:
        for official in symbols:
            instrument = official.strip().upper().replace("/", "")
            symbol = map_symbol(instrument)
            for year in years:
                imported = rows = weeks_ok = weeks_404 = weeks_failed = 0
                day_set: set[int] = set()
                for week in range(1, ISO_WEEKS + 1):
                    if not first_request and delay_ms > 0:
                        sleep_fn(delay_ms / 1000.0)
                    first_request = False
                    try:
                        text = fetch(instrument, year, week)
                    except Exception as e:
                        weeks_failed += 1
                        log(f"[失败] {instrument} {year} W{week:02d}: {e}")
                        continue
                    if text is None:
                        weeks_404 += 1
                        continue
                    try:
                        bars = parse_week_csv(text)
                    except ValueError as e:
                        weeks_failed += 1
                        log(f"[失败] {instrument} {year} W{week:02d}: {e}")
                        continue
                    weeks_ok += 1
                    if bars:
                        imported += store.insert_new_full_candles(symbol, TF_M1, bars)
                        rows += len(bars)
                        day_set.update(b[0] // 86400 for b in bars)
                days = len(day_set)
                results.append({
                    "instrument": instrument, "symbol": symbol, "year": year,
                    "imported": imported, "rows": rows, "days": days,
                    "avg_per_day": (imported / days) if days else 0.0,
                    "weeks_ok": weeks_ok, "weeks_404": weeks_404,
                    "weeks_failed": weeks_failed,
                })
    finally:
        store.close()
    return results


def print_backfill_summary(results: list[dict], db_path: str | Path,
                           log: Callable[[str], None] = print) -> None:
    log(f"== 官方 m1 导入统计 → {db_path} ==")
    total = 0
    for r in results:
        total += r["imported"]
        log(f"{r['instrument']} {r['year']} ({r['symbol']}): "
            f"新增 {r['imported']} 根 / 覆盖 {r['days']} 天 / 日均 {r['avg_per_day']:.1f} 根 | "
            f"有效周 {r['weeks_ok']} | 404 周 {r['weeks_404']} | 失败周 {r['weeks_failed']}")
    log(f"合计新增 {total} 根")


def pip_size(symbol: str) -> float:
    s = symbol.upper()
    if s.startswith("XAU") or s.startswith("XAG"):
        return 0.1
    return 0.01 if "JPY" in s else 0.0001


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def _open_ro(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro",
                           uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def compare_official_vs_main(main_db: str | Path, official_db: str | Path,
                             symbols: list[str]) -> list[dict]:
    main_conn = _open_ro(main_db)
    off_conn = _open_ro(official_db)
    reports: list[dict] = []
    try:
        for symbol in symbols:
            official = dict(off_conn.execute(
                "SELECT ts, close FROM candles WHERE symbol=? AND tf=? ORDER BY ts",
                (symbol, TF_M1)))
            report: dict = {"symbol": symbol, "overlap": 0}
            diffs: list[float] = []
            tss: list[int] = []
            if official:
                off_tss = list(official)
                start_ts, end_ts = off_tss[0], off_tss[-1]
                q = ("SELECT ts, close FROM candles "
                     "WHERE symbol=? AND tf=? AND ts BETWEEN ? AND ?")
                for ts, m_close in main_conn.execute(
                        q, (symbol, TF_M1, start_ts, end_ts)):
                    if ts in official:
                        tss.append(ts)
                        diffs.append(official[ts] - m_close)
                days = len({ts // 86400 for ts in tss})
                main_in_range = main_conn.execute(
                    "SELECT COUNT(*) FROM candles WHERE symbol=? AND tf=? AND ts BETWEEN ? AND ?",
                    (symbol, TF_M1, start_ts, end_ts)).fetchone()[0]
                srt = sorted(diffs)
                report.update(
                    days=days,
                    main_in_range=main_in_range,
                    coverage=(len(diffs) / main_in_range) if main_in_range else None,
                    start_ts=start_ts, end_ts=end_ts,
                    median=statistics.median(srt) if srt else None,
                    p90=_percentile(srt, 0.90) if srt else None,
                    p99=_percentile(srt, 0.99) if srt else None,
                    max_abs=max((abs(d) for d in srt), default=None),
                )
            else:
                report.update(days=0, main_in_range=0, coverage=None, start_ts=None,
                              end_ts=None, median=None, p90=None, p99=None, max_abs=None)
            report["overlap"] = len(diffs)
            reports.append(report)
    finally:
        main_conn.close()
        off_conn.close()
    return reports


def print_compare_reports(reports: list[dict], log: Callable[[str], None] = print) -> None:
    log("== 官方库 vs 主库 m1 基差对比（diff = 官方 - 主库，bid_close，价格单位）==")
    for r in reports:
        log(f"\n{r['symbol']}:")
        if not r["overlap"]:
            log("  无重叠数据（官方库与主库 m1 无共同 ts）")
            continue
        pip = pip_size(r["symbol"])
        cov = f"{r['coverage'] * 100:.1f}%" if r["coverage"] is not None else "n/a"
        log(f"  重叠区间 {_fmt_ts(r['start_ts'])} ~ {_fmt_ts(r['end_ts'])} UTC，"
            f"覆盖 {r['days']} 天（日均重叠 {r['overlap'] / r['days']:.1f} 根）")
        log(f"  重叠根数 {r['overlap']} / 主库区间根数 {r['main_in_range']} → 覆盖率 {cov}")
        log(f"  diff 中位 {r['median']:+.6f} ({r['median'] / pip:+.2f} pip) | "
            f"P90 {r['p90']:+.6f} ({r['p90'] / pip:+.2f} pip) | "
            f"P99 {r['p99']:+.6f} ({r['p99'] / pip:+.2f} pip) | "
            f"最大绝对 {r['max_abs']:.6f} ({r['max_abs'] / pip:.2f} pip)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FXCM 官方 m1 周文件下载/导入 + 官方库 vs 主库基差对比")
    p.add_argument("--symbols", default="EURUSD,USDJPY", help="官方品种名（逗号分隔，如 EURUSD,USDJPY）")
    p.add_argument("--years", default="2017-2026", help="年份: 2020 / 2017-2019 / 2021,2023,2025-2026")
    p.add_argument("--db", default=DEFAULT_DB, help="官方独立库路径")
    p.add_argument("--delay-ms", type=int, default=300, help="请求间隔毫秒")
    p.add_argument("--compare", action="store_true", help="只做官方库 vs 主库基差对比（不下载）")
    p.add_argument("--main-db", default=DEFAULT_MAIN_DB, help="主库路径（只读）")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    main_path = Path(args.main_db)
    if db_path.resolve() == main_path.resolve():
        p.error(f"--db 不能与主库相同（官方库必须独立，默认 {DEFAULT_DB}）")

    symbols_raw = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols_raw:
        p.error("--symbols 为空")
    try:
        symbols = [map_symbol(s) for s in symbols_raw]
    except ValueError as e:
        p.error(str(e))

    if args.compare:
        if not db_path.exists():
            print(f"错误: 官方库不存在: {db_path}（先跑下载导入）", file=sys.stderr)
            return 1
        try:
            reports = compare_official_vs_main(main_path, db_path, symbols)
        except sqlite3.OperationalError as e:
            print(f"错误: 打开数据库失败: {e}", file=sys.stderr)
            return 1
        print_compare_reports(reports)
        return 0

    try:
        years = parse_years(args.years)
    except ValueError as e:
        p.error(str(e))
    results = run_backfill(symbols_raw, years, db_path, delay_ms=args.delay_ms)
    print_backfill_summary(results, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
