"""10 年历史回填引擎：MarketDataSnapshot 分页（300 根/请求，query_depth 实测）。

说明：Price History API（fx.get_history）底层走 pricearchive HTTP 服务，本网络下不可用
（单请求 >160s）；快照路径走交易服务器通道（~1s/请求），故回填统一走快照分页。
分块方向：从最新向最旧游走，天然支持断点续传（游标 = 已回填的最早时间戳）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("fxcm_api.backfill")

# API 标签 → FXCM timeframe 标识
FXCM_TF: dict[str, str] = {"1m": "m1", "15m": "m15", "1h": "H1", "4h": "H4", "1d": "D1"}
TF_SECONDS: dict[str, int] = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
# 外层分块（秒）：内存与请求量的折中
CHUNK_SECONDS: dict[str, int] = {"1m": 30 * 86400, "15m": 120 * 86400,
                                 "1h": 365 * 86400, "4h": 365 * 86400,
                                 "1d": 10 * 365 * 86400}
MAX_SUBREQUESTS = 500000


def _epoch_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _row_ts(value) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(float(value))


def fetch_range(fx, symbol: str, tf_name: str, start_ts: int, end_ts: int) -> list[tuple]:
    """拉取 (start_ts, end_ts] 区间全量K线 → FullBar 元组列表（升序）。

    内部按 query_depth（实测 300）分页：每次取最接近 end 的 depth 根，游标向旧推进。
    """
    factory = fx.session.request_factory
    tf = factory.timeframe_collection.get(FXCM_TF[tf_name])
    depth = int(tf.query_depth)
    cursor_to = end_ts
    out: list[tuple] = []
    subrequests = 0
    while cursor_to > start_ts:
        subrequests += 1
        if subrequests > MAX_SUBREQUESTS:
            raise RuntimeError("快照分页超出安全阈值")
        req = factory.create_market_data_snapshot_request_instrument(symbol, tf, depth)
        factory.fill_market_data_snapshot_request_time(
            req, _epoch_to_dt(start_ts), _epoch_to_dt(cursor_to), True)
        reader = fx.send_request(req)
        n = reader.size
        time.sleep(0.1)                        # 分页间隔，避免连发触发服务端拖慢
        if n == 0:
            break
        earliest = None
        for i in range(n):
            ts = _row_ts(reader.get_date(i))
            out.append((ts,
                        reader.get_bid_open(i), reader.get_bid_high(i),
                        reader.get_bid_low(i), reader.get_bid_close(i),
                        reader.get_ask_open(i), reader.get_ask_high(i),
                        reader.get_ask_low(i), reader.get_ask_close(i),
                        int(reader.get_volume(i) or 0)))
            if earliest is None or ts < earliest:
                earliest = ts
        new_cursor = earliest - 1
        if new_cursor >= cursor_to:
            break                                  # 防御：服务端游标未前移
        cursor_to = new_cursor
        if n < depth:
            break                                  # 该窗口起点已无更多数据
    out = [b for b in out if start_ts < b[0] <= end_ts]
    out.sort(key=lambda b: b[0])
    return out


def _fetch_with_retry(fetch, fx, symbol, tf_name, start_ts, end_ts, retries=3):
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fetch(fx, symbol, tf_name, start_ts, end_ts)
        except Exception as exc:
            if "No data found" in str(exc):
                return []                        # 服务端明确无数据 → 空窗口（非瞬态）
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("fetch failed")


def probe(fx, symbol: str, tf_name: str, fetch=None) -> dict:
    """实测：单请求容量（query_depth）与 10 年内最早可得数据年份。"""
    fetch = fetch or fetch_range
    now = int(time.time())
    depth_tf = fx.session.request_factory.timeframe_collection.get(FXCM_TF[tf_name])
    depth = int(depth_tf.query_depth)
    recent_30d = len(fetch(fx, symbol, tf_name, now - 30 * 86400, now))
    recent_365d = len(fetch(fx, symbol, tf_name, now - 365 * 86400, now))
    earliest_year = None
    for year in range(10, 0, -1):                  # 从最旧往新找第一个非空年
        start = now - year * 365 * 86400
        bars = fetch(fx, symbol, tf_name, start, start + 365 * 86400)
        if bars:
            earliest_year = _epoch_to_dt(bars[0][0]).year
            break
    return {"symbol": symbol, "tf": tf_name, "query_depth": depth,
            "recent_30d": recent_30d, "recent_365d": recent_365d,
            "earliest_year": earliest_year}


def backfill(fx, symbol: str, tf_name: str, years: float, store,
             delay_ms: int = 300, progress=None, fetch=None) -> dict:
    """单品种单周期回填：从库内最新K线（或当前时间）向回走到 now - years 年。"""
    fetch = fetch or fetch_range
    tf_sec = TF_SECONDS[tf_name]
    now = int(time.time())
    target = now - int(years * 365 * 86400)
    latest = store.latest_ts(symbol, tf_sec)
    end = min(latest, now) if latest else now
    chunk = CHUNK_SECONDS[tf_name]
    total = 0
    requests = 0
    while end > target:
        start = max(end - chunk, target)
        try:
            bars = _fetch_with_retry(fetch, fx, symbol, tf_name, start, end)
        except Exception as exc:
            logger.error("回填 %s %s 区间[%s,%s]失败: %s", symbol, tf_name, start, end, exc)
            store.save_backfill_cursor(symbol, tf_sec, end, False)
            raise
        requests += 1
        if bars:
            store.upsert_full_candles(symbol, tf_sec, bars)
            total += len(bars)
            new_end = bars[0][0] - 1
            end = new_end if new_end < end else start - 1   # 防御：服务端游标未前移
        else:
            end = start - 1                  # 空窗口（周末/数据缺失）整体跳过
        if progress and (requests % 5 == 0 or end <= target):
            progress(symbol, tf_name, total, requests, end)
        time.sleep(delay_ms / 1000.0)
    store.save_backfill_cursor(symbol, tf_sec, end, True)
    return {"symbol": symbol, "tf": tf_name, "bars": total, "requests": requests,
            "earliest": store.earliest_ts(symbol, tf_sec)}


def backfill_all(fx, symbols: list[str], tf_labels: list[str], years: float, store,
                 delay_ms: int = 300, progress=None, fetch=None) -> list[dict]:
    results = []
    for symbol in symbols:
        for tf_name in tf_labels:
            logger.info("回填开始 %s %s（%s 年）", symbol, tf_name, years)
            r = backfill(fx, symbol, tf_name, years, store, delay_ms, progress, fetch)
            logger.info("回填完成 %s %s: %s 根 / %s 请求", symbol, tf_name,
                        r["bars"], r["requests"])
            results.append(r)
    return results
