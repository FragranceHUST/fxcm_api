# fxcm_api

FXCM 交易账户的动态止损管家与盯盘服务。

两个入口：

| 入口 | 说明 |
|---|---|
| `fxcm_api.main` | CLI 工具集：检查环境 / 行情流 / 手动开平仓 / 止损管理 |
| `fxcm_api.daemon` | 常驻进程：单次登录 + 实时行情聚合 + guard 止损循环 + 只读 Web 盯盘页 |

另有 `MQL4/Experts/OrderGuard.mq4`：同策略的 MT4 EA 版本（V1，固定点数）。

## 安装（macOS）

```bash
# 1) 用 Homebrew 的框架版 Python 建 venv（uv 托管的静态 Python 会与 forexconnect 的
#    内嵌 libpython 冲突导致段错误，勿用）
python3.10 -m venv .venv
.venv/bin/python -m ensurepip   # venv 缺 pip 时

# 2) 安装依赖（pythonhosted 被墙时走清华镜像）
uv pip install --python .venv/bin/python -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 3) 修补 forexconnect 的动态库路径（macOS 必需；重装包后需重跑）
bash scripts/patch_forexconnect_mac.sh
```

## 配置

复制 `config.example.json` 为 `config.json`（真实账户）或 `config.demo.json`（演示账户），填入凭据。两份真实配置已被 `.gitignore` 排除。

```jsonc
{
  "credentials": { "user_id": "...", "password": "...", "connection": "demo" },
  "guard": {
    "initial_sl_pips": 20.0,        // 开仓即挂的初始止损（0=不挂）
    "be_trigger_pips": 50.0,        // 浮盈达到该点数后推保本（XAU/USD 50 pips = 5.0 USD）
    "be_buffer_pips": 0.0,          // 保本位 = 开仓价 ± 该点数（0 = 精确成本价）
    "min_stop_distance_pips": 5.0,  // 新止损距市价的最小安全距离（过近则贴边钳制）
    "use_trailing": false,          // 移动止损（默认关）
    "poll_interval_ms": 500,        // 轮询周期
    "dry_run": true,                // true=只打印动作不发请求（观察模式）
    "pip_overrides": { "XAU/USD": 0.1 }   // 金属类 pip 定义
  },
  "daemon": {
    "watch_symbols": ["XAU/USD", "USD/JPY", "EUR/USD"],
    "port": 8911,
    "data_dir": "data",
    "guard_enabled": true
  }
}
```

止损策略（与 MQL4 版对齐）：INIT（初始止损）→ BE（浮盈达标推到成本价）→ TRAIL（可选）。止损只向有利方向改善，绝不放宽。

## CLI

```bash
.venv/bin/python -m fxcm_api.main check    --config config.demo.json   # 环境自检
.venv/bin/python -m fxcm_api.main login    --config config.demo.json   # 登录验证
.venv/bin/python -m fxcm_api.main stream   --config config.demo.json --symbol EUR/USD --seconds 10
.venv/bin/python -m fxcm_api.main measure  --config config.demo.json --seconds 60  # 行情速率实测
.venv/bin/python -m fxcm_api.main guard    --config config.demo.json   # 前台跑止损管家
.venv/bin/python -m fxcm_api.main positions --config config.demo.json  # 持仓一览
.venv/bin/python -m fxcm_api.main open/close/sl ...                    # 交易验证工具
```

## Daemon + Web 盯盘页

```bash
.venv/bin/python -m fxcm_api.daemon --config config.demo.json
# 浏览器打开 http://127.0.0.1:8911/
```

- **单次登录**：会话、行情订阅、guard 循环、Web 服务共享同一会话，避免 CLI 反复登录触发 FXCM 节流
- **行情聚合**：OFFERS 更新事件 → 1s/1m/15m/1h K线（线程安全聚合器）
- **本地 CSV**：已收盘 1s K线逐根追加至 `data/<品种>_1s.csv`；重启自动加载恢复
- **Web 页面（只读）**：三品种切换、四周期K线（TradingView Lightweight Charts 本地打包）、实时买卖价与点差；REST + WebSocket，无任何下单接口
- API：`/api/health`、`/api/quotes`、`/api/candles?symbol=XAU/USD&tf=1s|1m|15m|1h`、`/api/positions`、`/ws`

注意：daemon 需在仓库根目录启动（`static/` 与 `data/` 按相对路径解析）。会话断线后的自动重连属后续迭代。

## 测试

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```
