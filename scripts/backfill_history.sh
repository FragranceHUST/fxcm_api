#!/usr/bin/env bash
# 分段历史回填：阶段1 补停机缺口(m1/m15 近7天) → 阶段2 十年 m1/m15 → 阶段3 补 D1
# 用法: scripts/backfill_history.sh [config] [logfile]
# 断点续传：中断后重跑本脚本即从库内最新K线继续，无需清理
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
CFG=${1:-config.json}
LOG=${2:-/tmp/backfill_history.log}
DELAY=${DELAY_MS:-250}

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "===== 分段回填启动 config=$CFG delay=${DELAY}ms ====="

log "阶段1：缺口回填 m1/m15 近7天（优先恢复盯盘数据连续性）"
PYTHONPATH=. $PY -m fxcm_api.main --config "$CFG" backfill \
  --tf 1m,15m --years 0.02 --delay-ms "$DELAY" >> "$LOG" 2>&1
log "阶段1退出码=$?，进入阶段2"

log "阶段2a：十年 m1/m15（USD/JPY、EUR/USD 先行，快速探明深度）"
PYTHONPATH=. $PY -m fxcm_api.main --config "$CFG" backfill \
  --tf 1m,15m --years 10 --delay-ms "$DELAY" --symbols "USD/JPY" "EUR/USD" >> "$LOG" 2>&1
log "阶段2a退出码=$?，进入阶段2b"

log "阶段2b：十年 m1/m15（XAU/USD 深度最大，压轴过夜跑）"
PYTHONPATH=. $PY -m fxcm_api.main --config "$CFG" backfill \
  --tf 1m,15m --years 10 --delay-ms "$DELAY" --symbols "XAU/USD" >> "$LOG" 2>&1
log "阶段2b退出码=$?，进入阶段3"

log "阶段3：补 D1（USD/JPY、EUR/USD）"
PYTHONPATH=. $PY -m fxcm_api.main --config "$CFG" backfill \
  --tf 1d --years 10 --delay-ms "$DELAY" --symbols "USD/JPY" "EUR/USD" >> "$LOG" 2>&1
log "阶段3退出码=$?，全部完成"
