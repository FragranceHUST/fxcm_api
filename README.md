# fxcm_api

FXCM 交易账户的动态止损管家、双环境盯盘与交易服务。

| 入口 | 说明 |
|---|---|
| `fxcm_api.main` | CLI 工具集：环境自检 / 行情速率实测 / 手动交易 / 历史回填；daemon 在线时自动走 HTTP 代理（零登录） |
| `fxcm_api.daemon` | 常驻进程：real+demo 双会话、行情聚合落库、guard 止损循环、触发式入场、Web 盯盘交易页 |

> 项目的 MT4 EA 前身（`MQL4/Experts/OrderGuard.mq4`，V1 固定点数）已移除：Python daemon 是唯一止损管家，避免同账号双管家冲突。

## 架构

```
real 会话（数据面，唯一）──OFFERS 订阅──> 内存聚合(1s) ──> SQLite(candles.db) ──> 图表/统计（两环境共用）
                └─get_history 分块──> 10 年历史回填（1m/15m/1h/4h/1d）
demo 会话（账户面）──> guard 循环 + demo 交易/账户
real 会话（账户面）──> guard 循环(可选) + real 交易/账户
```

- 单进程双会话（各自监督线程、错峰登录 ≥10s、断线自愈：RECONNECTING 等待 / 其余状态退避重连 15→30→60→120s）
- guard 按环境参数化：demo 全品种；real 建议仅 XAU/USD 专属参数（见下）

## 安装（macOS）

```bash
# 1) 必须用框架版 Python（brew 的 python@3.10 或 python.org 官方安装包）。
#    uv 托管的静态 Python 会与 forexconnect 内嵌 libpython 冲突导致段错误，勿用
python3.10 -m venv .venv
.venv/bin/python -m ensurepip   # venv 缺 pip 时

# 2) 安装依赖（pythonhosted 被墙时走清华镜像）
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 3) 修补 forexconnect 轮子的硬编码库路径（macOS 必需；重装包后需重跑）
bash scripts/patch_forexconnect_mac.sh
# 若本机是 python.org 框架版 Python（轮子本身的构建框架，libpython 路径无需改），
# 只需显式传入路径让脚本继续修 libz：
bash scripts/patch_forexconnect_mac.sh /Library/Frameworks/Python.framework/Versions/3.10/Python

# 4) 自检
.venv/bin/python -m fxcm_api.main check
```

## 配置

复制 `config.example.json` 为 `config.json`（real）与 `config.demo.json`（demo）。真实配置已被 `.gitignore` 排除；`FXCM_USER_ID` / `FXCM_PASSWORD` 环境变量优先于文件。

```jsonc
{
  "credentials": {
    "user_id": "...", "password": "...",
    "url": "www.fxcorporate.com/Hosts.jsp",
    "connection": "demo",            // demo / real
    "session_id": "", "pin": ""      // 多数据库用户/PIN 用户才需要
  },
  "guard": {
    "initial_sl_pips": 20.0,         // 开仓即挂的初始止损（0=不挂）
    "be_trigger_pips": 50.0,         // 浮盈达该点数后推保本（XAU/USD 50 pips = 5.0 USD）
    "be_buffer_pips": 0.0,           // 保本位 = 开仓价 ± 该点数（0 = 精确成本价）
    "use_trailing": false,           // 移动止损（默认关）
    "trail_start_pips": 10.0, "trail_dist_pips": 8.0, "trail_step_pips": 1.0,
    "min_stop_distance_pips": 5.0,   // 新止损距市价的最小安全距离（过近则贴边钳制）
    "poll_interval_ms": 500,         // 轮询周期
    "dry_run": true,                 // true=只打印动作不发请求（观察模式）
    "symbol_filter": null,           // null=全品种；如 ["XAU/USD"]
    "pip_overrides": { "XAU/USD": 0.1 },   // 金属类 pip 定义
    "account_id": ""                 // 空=自动取账户表第一行
  },
  "daemon": {
    "watch_symbols": ["XAU/USD", "USD/JPY", "EUR/USD"],
    "port": 8911,
    "data_dir": "data",
    "guard_enabled": true,           // 本环境 guard 循环开关（第二台监控机设 false）
    "allow_trading": true            // 本环境是否允许下单
  }
}
```

**real 环境（高杠杆账户）建议 guard**（XAU/USD 专属，`symbol_filter: ["XAU/USD"]`）：

| 参数 | demo（已实测） | real |
|---|---|---|
| 初始止损 | 20 pips（$2.00） | 150 pips（$15.00） |
| 保本触发 | 50 pips（$5.00） | 51 pips（$5.10） |
| 保本位 | 成本价 | 成本价 + 0.1USD（1 pip 缓冲覆盖滑点） |

止损策略：INIT（初始止损）→ BE（浮盈达标推保本）→ TRAIL（可选移动）。**止损只向有利方向改善，绝不放宽**；发单后 5s 在途窗口防重复下单竞态，服务端 ORA-20114 重复单降级为良性事件。

## CLI

```bash
V=.venv/bin/python
$V -m fxcm_api.main check     # 环境自检（不登录）
$V -m fxcm_api.main login     --config config.demo.json        # 登录验证 + guard 摘要
$V -m fxcm_api.main stream    --config config.demo.json --symbol EUR/USD --seconds 10
$V -m fxcm_api.main measure   --config config.demo.json --symbols XAU/USD EUR/USD --seconds 60  # 行情速率实测（事件计数）
$V -m fxcm_api.main positions --env demo                       # 持仓一览（daemon 在线时零登录代理）
$V -m fxcm_api.main open      --env demo --symbol XAU/USD --buy --amount 10
$V -m fxcm_api.main close     --env demo --trade-id 225455313 [--amount 5]
$V -m fxcm_api.main sl        --env demo --trade-id 225455313 --pips 10   # 或 --price 绝对价
$V -m fxcm_api.main history   --env demo --limit 50            # 已平仓交易
$V -m fxcm_api.main probe-history --symbol XAU/USD --tf 1m     # 实测单请求容量与可回溯深度
$V -m fxcm_api.main backfill  --symbols XAU/USD USD/JPY EUR/USD --tf 1m,15m,1h,4h,1d --years 10
```

- **会话复用**：`positions/open/close/sl/history` 先探测 daemon（`127.0.0.1:8911`），在线则走 HTTP 代理（不再登录，避免触发 FXCM 节流）；离线回退直连（需 `--config`）
- **real 安全门**：CLI 对 real 环境强制 `yes` 二次确认
- **`guard` 子命令**：daemon 在线时自动停用并提示（避免双管家）；仅离线应急用

## Daemon + Web

```bash
.venv/bin/python -m fxcm_api.daemon --config config.demo.json --config-real config.json
# 浏览器打开 http://127.0.0.1:8911/ ；需在仓库根目录启动（static/、data/ 按相对路径解析）

# 原生层静默崩溃的监督重启（退避 30s/60s/120s 封顶）
scripts/run_daemon.sh config.demo.json config.json
```

- **多端访问**：固定一台常开机器跑 daemon（`--host 0.0.0.0`），手机/笔记本浏览器访问 `http://主机IP:8911`。第二台设备如需再跑 daemon，其配置必须 `"guard_enabled": false`——**同一账户同一时刻只允许一个管家**，否则会出现 INIT 双发竞态、触发式入场双发（双倍仓位）、本地状态（游标/触发器/净值采样）分裂等冲突；`--host 0.0.0.0` 会把可下真实订单的 API 暴露到局域网，公共网络注意限制来源 IP

### Web 页面（static/index.html）

- 顶栏 `[模拟 DEMO | 真实 REAL]` 切换：DEMO 蓝绿主题；REAL 红橙主题 + 常驻「⚠ 真实资金」横幅
- 三品种 K 线（TradingView Lightweight Charts 本地打包），周期 1m/15m/1h/4h；历史读 SQLite、最近读内存实时条，无缝拼接；数据两环境共用 real 会话
- 每环境独立：账户卡（余额/净值/占用保证金）、持仓表（平仓按钮）、挂单表 + 触发器（撤销按钮）、已平仓记录、统计卡
- 交易面板：市价（可选 range/SL/TP）、限价/止损入场（自动路由：有利侧→原生 LIMIT_ENTRY；突破侧→客户端触发式 band+range）；下单前展示安全手数（保证金 ≤ 净值 50% 口径）
- **安全门控**：环境级 `allow_trading` 总开关；real 环境所有写操作强制二次确认（后端 428 门控，UI 弹窗）

### REST API

| 端点 | 说明 |
|---|---|
| `GET /api/health` `/api/environments` `/api/quotes` `/api/candles?symbol=&tf=&limit=` | 行情与会话状态（tf: 1s/1m/15m/1h/4h/1d） |
| `GET /api/{env}/positions` `GET /api/{env}/orders` | 持仓；挂单+触发器 |
| `POST /api/{env}/orders` | 下单（`order_type: market/limit/stop`；real 需 `confirm: true`） |
| `POST /api/{env}/positions/{id}/close` `PATCH /api/{env}/positions/{id}/sl` | 平仓（可部分）/改损 |
| `DELETE /api/{env}/orders/{id}` `DELETE /api/{env}/triggers/{id}` | 撤单/撤触发器 |
| `GET /api/{env}/trade-constraints?symbol=` | 最小手数/安全手数上限 |
| `GET /api/{env}/stats` `/api/{env}/history/trades|orders|messages` | 统计 / 已平仓 / 订单日志 / 服务器消息 |
| `WS /ws` | 实时报价推送（tick 事件驱动，到达即推 + 5s 心跳兜底） |

## 数据模块（fxcm_api/data/）

SQLite 单文件 `data/candles.db`（WAL），四张表：

| 表 | 内容 |
|---|---|
| `candles` | K 线：bid OHLC + tick volume（ask 四价保留可 SQL 直查）；主键 (symbol, tf, ts) |
| `equity_samples` | 净值采样（daemon 内 30s 一采：balance/equity/margin_used） |
| `order_journal` | 订单日志：每笔请求与成交（请求价 vs 成交价 → 滑点统计），触发器重启自动恢复 |
| `backfill_cursor` | 回填游标（断点续传） |

- **统一读取入口**：`CandleStore.get_candles(symbol, tf, start_ts, end_ts, limit)`——实盘与工具从这里取数；量化回测（quant/）用只读 SQL 直连（numpy 数组形态，WAL 共存），大数据量回测不经对象化封装
- **回填引擎**：走 MarketDataSnapshot 快照分页（单请求 300 根，走交易服务器通道；Price History API 底层 pricearchive 在被墙网络不可用），从最新向最旧游走、按 300ms 限速、`Ctrl+C` 后重跑同一命令即续传，服务端 "No data found" 视为空窗口跳过
- **实时接入**：OFFERS 更新 → 内存多周期聚合（1s 仅内存、实时刷新用，不落库）；1m/15m/1h/4h 收盘即落库，H4 为 FXCM 原生周期
- 周期标识：API 用 `1m/15m/1h/4h/1d`，回填/存储同套标识（`1s` 仅实时层）

## 统计（fxcm_api/data/stats.py）

CostFunction.h 14 指标全量移植：胜率、盈亏比、最大连续盈/亏、平均持仓时长、已实现/浮动/总 PnL、总佣金、最大回撤、年化夏普（日收益序列，基准 0=信息比率）、滑点 MSE/绝对误差/总滑点（来自订单日志）、按日 PnL。回撤优先用净值采样曲线，缺失时按已平仓累计重建。FXCM 无真实成交量（OTC 市场），`volume` 为 tick volume 口径。

## 测试

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests   # 65 例离线单测
```

## Linux / Docker

**当前被阻塞**：forexconnect 1.6.43 对 py3.10 仅发布 macOS-arm64 wheel（无 sdist），fxcorepy Boost.Python 绑定无 Linux 产物；应用代码本身 100% 跨平台。三条解除路径与结构就绪的 `Dockerfile` 见 `docs/DOCKER_SPIKE.md`（首选：向 devel@fxcm.com 索取 Linux wheel）。

## 已知约束

- 品种名带斜杠（`XAU/USD`），URL 参数需编码
- FXCM 对频繁登录有节流（Wait timeout）——交易操作请走 daemon（CLI 会自动代理），不要反复直连
- daemon 需在仓库根目录启动；`static/`、`data/`、`History/` 按相对路径解析
- 服务端仅保留近期已平仓交易，长期交易历史靠本地 `order_journal` 从上线起累积
