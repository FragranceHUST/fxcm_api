#!/bin/zsh
# daemon 监督脚本：原生层静默崩溃时自动重启（退避 30s/60s/120s，循环封顶 120s）。
# 用法: scripts/run_daemon.sh [demo配置] [real配置]
# 前台运行；建议 nohup 或 tmux 内使用。Ctrl+C 退出后不再重启。

DEMO_CFG=${1:-config.demo.json}
REAL_CFG=${2:-config.json}
BACKOFF=30

while true; do
  echo "[supervisor] $(date '+%F %T') 启动 daemon..."
  PYTHONUNBUFFERED=1 .venv/bin/python -m fxcm_api.daemon \
    --config "$DEMO_CFG" --config-real "$REAL_CFG"
  rc=$?
  echo "[supervisor] $(date '+%F %T') daemon 退出 rc=$rc，${BACKOFF}s 后重启"
  if [ $rc -eq 0 ] || [ $rc -eq 1 ]; then
    echo "[supervisor] 正常退出（0）或 real 登录失败（1），停止监督"
    break
  fi
  sleep $BACKOFF
  BACKOFF=$(( BACKOFF < 120 ? BACKOFF * 2 : 120 ))
done
