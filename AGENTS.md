# AGENTS.md

fxcm_api — FXCM 交易账户的动态止损管家与盯盘服务（MT4 EA + ForexConnect Python）。

## Sub-agent policy（用户强制约定）
- Sub-agent tasks (task tool): default to **blocking/sync** execution (`run_in_background=false`), so results return inline. Use background mode only when the user explicitly approves parallel execution.

## 环境陷阱（macOS，必读）
- `.venv` 必须基于 **Homebrew 框架版 Python 3.10**。uv 托管的静态 Python 会因双 libpython 冲突与 forexconnect 内嵌库**段错误**（无报错、进程直接消失）。
- `.venv` 无内置 pip。安装依赖用：`uv pip install --python .venv/bin/python -i https://pypi.tuna.tsinghua.edu.cn/simple <pkg>`（本网络 pythonhosted 被墙）。
- **重装 forexconnect 后必须重跑** `bash scripts/patch_forexconnect_mac.sh`（修补 6 个二进制的 dylib 路径并 ad-hoc 重签名）。漏跑 = 无声原生崩溃。

## 常用命令
- 测试（全离线，不登录）：`PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`
- CLI：`.venv/bin/python -m fxcm_api.main --config config.demo.json <check|describe|login|stream|measure|guard|positions|open|close|sl>`
- Daemon：`.venv/bin/python -m fxcm_api.daemon --config config.demo.json` → http://127.0.0.1:8911，**必须在仓库根目录启动**（static/ 与 data/ 按相对路径解析）
- 类型检查：basedpyright（pyrightconfig.json）；单测某个类：`PYTHONPATH=. .venv/bin/python -m unittest tests.test_stops.TestDryRun`

## FXCM / forexconnect 高价值坑
- **FXCM 节流登录**：短时间多次登录触发 `Wait timeout exceeded`，重试期 forexconnect 原生层可能**无声退出**（日志为空、无 crash report）。daemon 持有唯一会话（行情+guard+web 共享），**不要循环调用 CLI**——每次 CLI 调用都是一次完整登录。
- TRADES 行**没有 `is_buy` 字段**，方向在 `buy_sell`（'B'/'S'）；统一用 `fxcm_api.trading.trade_is_buy(row)`。
- `row.columns` 元素是 `O2GTableColumn` 对象（用 `col.id`），直接 join 会崩。
- 包装器 `logout()` 结束时把内部 `_session` 置 None——**先抓 `fx.session` 引用再 logout**（session.py 已处理）。
- guard 工作线程里调 `send_request` 是安全的；wrapper 的 "not from main thread" 警告只针对 fxcorepy 回调内调用。
- `ORA-20114`（同订单组同类型订单）= 服务器已有止损单（表格刷新延迟），StopManager 已按良性事件处理 + 5 秒在途窗口防重发——不要改回 ERROR 级日志。
- 止损规则：**只向有利方向改善，绝不放宽**；`min_stop_distance_pips` 距市价钳制；XAU/USD 的 pip = 0.1（`pip_overrides`）。
- 品种名含 `/`（如 `EUR/USD`）：HTTP API 里品种走 **query 参数**（路径参数会被 Starlette 解码后路由失败）；CSV 文件名把 `/` 换成 `_`。
- **OFFERS `pip_size` 可能错 10×**（本 demo EUR/USD 解析出 0.001，真实 pip 0.0001）：凡 pip 计量的 guard 距离（BE 触发/min_stop）必须用 `pip_overrides` 显式钉死；价格空间计算（ATR/带/TP/SL）不受影响。
- 本地 candles 库的 **4h 表混有服务器原生桶对齐**（回填产物，非 epoch 对齐）：策略 ATR 一律从 m1 重采样（`fxcm_api/strategy_runner.py: resample_h4`），勿直接读 4h 表。
- 官方 API 细节优先查本地 `docs/forexconnect_api_reference.md`（经源码核验）与 `scripts/dump_api_reference.py` 内省工具。

## LSP 误报（已用源码证据验证，勿"修复"）
- `from forexconnect import fxcorepy` 报未知导入符号。
- `create_order_request(**kwargs: str)` 标注不准：运行时按类型分发（float RATE → set_double），float 传参是正确的。

## Git
- 22 端口被墙，SSH 走 `ssh.github.com:443`（~/.ssh/config 已配，密钥 `~/.ssh/fxcm_github_ed25519`）。
- 提交身份为仓库级 `FragranceHUST <FragranceHUST@users.noreply.github.com>`（勿动全局）。
- **凭据永不入库**：config.json / config.demo.json / data/ 已 gitignore；涉及配置的提交前用 `git check-ignore` 复核。
- 提交风格：中文主题行、按模块原子提交（参考 git log）。

## 代码约定
- 注释只在必要时写（有 hook 强制最小注释策略，冗余注释会被要求删除）。
- 测试为纯逻辑单测（不登录 FXCM）；实盘/实demo 验证靠 daemon 端点：`curl 127.0.0.1:8911/api/{health,quotes,positions,candles}`。
- daemon 内置 guard 循环（`daemon.guard_enabled`）；**不要与 CLI `guard` 命令同时运行**（两个管家会互相抢活）。
