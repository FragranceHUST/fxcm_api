"""一次性修复：candles.db 回填行时间戳 -8h 错位迁移。

根因：fxcm_api/data/backfill.py::_row_ts 曾把 fxcorepy get_date 的 naive GMT
按本地时区（UTC+8）解释，导致快照回填写入的所有行（ask 四价非空，live 路径
只写 bid、ask 恒为 NULL，可作判别器）时间戳整体 -8h。本脚本将这些行 +shift
迁回真实坐标，并同步平移 backfill_cursor。

用法（仓库根目录，迁移前必须停掉 daemon）:
  .venv/bin/python scripts/fix_backfill_tz.py                 # dry-run：只统计与方向校验
  .venv/bin/python scripts/fix_backfill_tz.py --apply         # 执行迁移（自动先备份）
  .venv/bin/python scripts/fix_backfill_tz.py --apply --vacuum
"""

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SHIFT_SECONDS = 8 * 3600
MATCH_TOL = 2e-4
_BACKFILL_COLS = ("symbol, tf, ts, open, high, low, close, "
                  "ask_open, ask_high, ask_low, ask_close, volume")
_ASK_POPULATED = "ask_open IS NOT NULL AND ask_open != ''"


def find_daemon_pid() -> int | None:
    out = subprocess.run(["pgrep", "-f", "fxcm_api.daemon"],
                         capture_output=True, text=True)
    pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    return pids[0] if pids else None


def count_backfill_rows(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        f"SELECT tf, COUNT(*) FROM candles WHERE {_ASK_POPULATED} GROUP BY tf"
    ).fetchall()
    return dict(rows)


def detect_needed_shift(main_db: Path, official_db: Path) -> int | None:
    """用官方库判定主库回填行当前处于哪个坐标系：需要 +shift / 已正确(0) / 无法判定(None)。"""
    if not official_db.exists():
        return None
    off = sqlite3.connect(f"file:{official_db}?mode=ro", uri=True)
    omin, omax = off.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol='EUR/USD' AND tf=60"
    ).fetchone()
    main = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    candidates = main.execute(
        f"SELECT ts, close FROM candles WHERE symbol='EUR/USD' AND tf=60 "
        f"AND {_ASK_POPULATED} AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 8",
        (omin + SHIFT_SECONDS, omax - SHIFT_SECONDS),
    ).fetchall()
    main.close()
    off.close()
    votes_shift = votes_same = 0
    for ts, close in candidates:
        off_shift = off_close_at(official_db, ts + SHIFT_SECONDS)
        off_same = off_close_at(official_db, ts)
        if off_shift is None or off_same is None:
            continue
        if abs(close - off_shift) <= MATCH_TOL and abs(close - off_same) > MATCH_TOL:
            votes_shift += 1
        elif abs(close - off_same) <= MATCH_TOL and abs(close - off_shift) > MATCH_TOL:
            votes_same += 1
    if votes_shift >= 6:
        return SHIFT_SECONDS
    if votes_same >= 6:
        return 0
    return None


def off_close_at(official_db: Path, ts: int) -> float | None:
    off = sqlite3.connect(f"file:{official_db}?mode=ro", uri=True)
    try:
        row = off.execute(
            "SELECT close FROM candles WHERE symbol='EUR/USD' AND tf=60 AND ts=?",
            (ts,)).fetchone()
    finally:
        off.close()
    return row[0] if row else None


def migrate(db: Path, shift: int) -> dict:
    conn = sqlite3.connect(str(db), timeout=30.0)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = count_backfill_rows(conn)
    conn.execute("BEGIN")
    conn.execute("CREATE TEMP TABLE shifted AS "
                 f"SELECT {_BACKFILL_COLS} FROM candles WHERE {_ASK_POPULATED}", ())
    conn.execute(f"DELETE FROM candles WHERE {_ASK_POPULATED}")
    before_changes = conn.total_changes
    conn.execute(f"INSERT OR IGNORE INTO candles ({_BACKFILL_COLS}) "
                 "SELECT symbol, tf, ts + ?, open, high, low, close, "
                 "ask_open, ask_high, ask_low, ask_close, volume FROM shifted",
                 (shift,))
    inserted = conn.total_changes - before_changes
    conn.execute("DROP TABLE shifted")
    cur = conn.execute("UPDATE backfill_cursor SET earliest_ts = earliest_ts + ? "
                       "WHERE earliest_ts IS NOT NULL", (shift,))
    cursors = cur.rowcount
    conn.commit()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    return {"before": before, "inserted": inserted,
            "conflicts": sum(before.values()) - inserted,
            "cursors": cursors, "integrity": integrity}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="candles.db 回填行时间戳 -8h 错位迁移")
    parser.add_argument("--db", default="data/candles.db")
    parser.add_argument("--official", default="data/candles_official.db")
    parser.add_argument("--apply", action="store_true", help="执行迁移（默认 dry-run）")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--vacuum", action="store_true", help="迁移后 VACUUM（耗时且需双倍磁盘）")
    parser.add_argument("--force", action="store_true", help="忽略 daemon 进程检测与方向校验失败")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"[x] 数据库不存在: {db}")
        return 1
    pid = find_daemon_pid()
    if pid and not args.force:
        print(f"[x] 检测到 daemon 进程在运行 (pid={pid})，迁移必须先停 daemon；"
              f"确认已停则加 --force")
        return 1

    conn = sqlite3.connect(str(db))
    counts = count_backfill_rows(conn)
    total = sum(counts.values())
    conn.close()
    print(f"[i] 回填行（ask 非空）按 tf 分布: {counts}，合计 {total}")
    if total == 0:
        print("[i] 无待迁移行，退出")
        return 0

    detected = detect_needed_shift(db, Path(args.official))
    if detected == 0:
        print("[x] 方向校验：主库回填行与官方库同坐标吻合，疑似已迁移过，拒绝执行"
              "（确认需要请 --force）")
        return 1
    if detected is None:
        if not args.force:
            print("[x] 方向校验：官方库缺失或样本无法判定，拒绝执行（--force 跳过校验）")
            return 1
        print("[!] 方向校验跳过（--force）")
    else:
        print(f"[i] 方向校验通过：回填行整体 -{detected // 3600}h 错位，将 +{detected // 3600}h 迁移")

    if not args.apply:
        print("[i] dry-run 完成，未写入。确认执行请加 --apply")
        return 0

    backup = db.with_name(f"{db.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    if not args.no_backup:
        src = sqlite3.connect(str(db))
        src.execute(f"VACUUM INTO '{backup}'")
        src.close()
        print(f"[i] 备份完成: {backup} ({backup.stat().st_size / 1e6:.0f} MB)")

    result = migrate(db, detected if detected else SHIFT_SECONDS)
    print(f"[i] 迁移完成: 迁移 {sum(result['before'].values())} 行，"
          f"写入 {result['inserted']}，冲突跳过 {result['conflicts']}，"
          f"游标平移 {result['cursors']} 条，integrity={result['integrity']}")

    if args.vacuum:
        print("[i] VACUUM 中（可能需要数分钟）...")
        conn = sqlite3.connect(str(db))
        conn.execute("VACUUM")
        conn.close()
        print(f"[i] VACUUM 完成，当前大小 {db.stat().st_size / 1e6:.0f} MB")

    print("[i] 后续步骤: 用修复后的代码重启 daemon，启动补洞会自动回填真实缺口；"
          "可重跑官方基差对比验证（修复后基差应≈0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
