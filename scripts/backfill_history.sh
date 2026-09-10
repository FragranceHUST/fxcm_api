#!/usr/bin/env bash
# 分段历史回填 v2：深度续挖(resume-floor，不重扫已存区间) → D1 → 顶部补齐
# 单阶段失败自动冷却重试（账户节流恢复）；阶段间强制间隔，保护登录配额
# 用法: scripts/backfill_history.sh [config] [logfile]
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
CFG=${1:-config.json}
LOG=${2:-/tmp/backfill_history.log}
DELAY=${DELAY_MS:-250}
COOLDOWN=${COOLDOWN_S:-1200}   # 阶段失败后的冷却秒数（节流恢复）
MAXTRY=${MAXTRY:-3}
GAP_S=${GAP_S:-120}            # 阶段间隔

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

run_phase() {  # $1=名称 $2=最大尝试次数 其余=命令
  local name=$1 tries=$2; shift 2
  local i rc
  for i in $(seq 1 "$tries"); do
    log "$name 第${i}/${tries}次尝试"
    PYTHONPATH=. "$@" >> "$LOG" 2>&1
    rc=$?
    log "$name 退出码=$rc"
    [ "$rc" -eq 0 ] && return 0
    [ "$i" -lt "$tries" ] && log "$name 失败，冷却 ${COOLDOWN}s" && sleep "$COOLDOWN"
  done
  return 1
}

BACK="$PY -m fxcm_api.main --config $CFG backfill --delay-ms $DELAY"

log "===== 分段回填 v2 启动 config=$CFG ====="
run_phase "深度-USDJPY-EURUSD" "$MAXTRY" \
  $BACK --tf 1m,15m --years 10 --resume-floor --symbols "USD/JPY" "EUR/USD"
sleep "$GAP_S"
run_phase "深度-XAUUSD" "$MAXTRY" \
  $BACK --tf 1m,15m --years 10 --resume-floor --symbols "XAU/USD"
sleep "$GAP_S"
run_phase "补D1" "$MAXTRY" \
  $BACK --tf 1d --years 10 --resume-floor --symbols "USD/JPY" "EUR/USD"
sleep "$GAP_S"
run_phase "顶部补齐" "$MAXTRY" \
  $BACK --tf 1m,15m,1d --years 0.02 --symbols "XAU/USD" "USD/JPY" "EUR/USD"
log "===== 全部结束 ====="
