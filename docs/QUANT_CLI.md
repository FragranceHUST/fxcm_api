# 回测系统 CLI 参数文档（quant/）

> 适用版本：2026-09-13（V2 二维扫参之后）。所有窗口时间均为 UTC；`--end` 语义 = 排他（数据截至前一日最后一根 m1）。

## 1. 子命令总览

| 命令 | 用途 | 典型耗时 |
|---|---|---|
| `verify` | M0 数据完备性审计（缺口+覆盖率，周末桥豁免上限 4 天） | ~30s/库 |
| `backtest` | 单参数集单次回测 | 1-2s |
| `sweep` | param1 × [param2] × 成本档全网格 + buy-hold 基准 + JSON/CSV/Excel/热力图 | 51×191×3 ≈ 29,223 run ≈ 15-20 min（9 workers） |
| `wfa` | Walk-Forward：滚动 train/test 折 + 折内选参 + OOS 拼接 | 10 折 × 51×191 ≈ 97,410 run ≈ 20-30 min |

## 2. 通用语义

- **数据路由**（`--db` 缺省时自动）：官方库 `data/candles_official.db` 有该品种 m1 → 用官方库；否则回退主库 `data/candles.db`（账户实价）。XAU/USD 官方源无数据，自动落主库（⚠️ 分钟覆盖 ~40%）。
- **成本**：`--spread-rt` 为**价格单位**的往返点差（USD/JPY 1.0 pip=0.01；EUR/USD 1.0 pip=0.0001；XAU $0.35=3.5 pips）；sweep 的 `--cost-levels 1,2,3` 对其乘倍数。
- **损益换算**：USD 报价品种恒 1；JPY 报价按出场 bar 汇率（`quote_unit_value` 单一来源）。
- **交易语义（锁定批复，勿改）**：触价制（D2）、带沿理想价成交（B3）、同根双触 SL 优先（B2）、单向单持仓（D4）、期末 eod 强平、跳空穿越按开盘价。
- **输出**：`<out>/{品 种}_{起}_{止}_{sweep|wfa}.json/.csv`；`--xlsx` 追加 sheet（同名自动 -2 去重），网格行按 sharpe 降序、基准行恒末行。

## 3. `backtest`

```
python -m quant.cli backtest --symbol USD/JPY --start 2020-01-01 --end 2026-09-11 \
    --strategy strategies/vol_reversal.py --param param1=0.58 --param sl_pips=35 \
    --quantity 50000 --spread-rt 0.01 --out result.json
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--symbol` | XAU/USD | 品种（带斜杠） |
| `--years` | 3.0 | 相对窗口（当前时刻往回）；与 `--start/--end` 互斥（后者优先） |
| `--start` / `--end` | — | ISO 日期；end 排他 |
| `--direction` | long | long/short/both |
| `--quantity` | 1 | 品种原生单位（外汇=基础货币单位，0.5 手=50000；XAU=盎司，0.5 手=50） |
| `--spread-rt` | 0.35 | 往返点差（价格单位） |
| `--param k=v` | — | 策略参数，可重复 |
| `--out` | — | JSON 落盘（含逐笔 trades） |

## 4. `sweep`

`backtest` 的全网格版。行字段 23 列：trades/winrate/sharpe/信息比率/盈亏比/最大回撤/最大连盈连亏/佣金/滑点/已实现与总 PnL/return_pct/annualized_return_pct(CAGR)/profit_factor/avg_holding_hours/pnl_by_year（按出场年）等。

| 参数（特有） | 默认 | 说明 |
|---|---|---|
| `--param1-start/-stop/-step` | 0.0/1.0/0.02 | 51 点网格 |
| `--param2-name` | — | **第二维参数名**（如 `tp_atr_mult`）；缺省=单维扫参（v1 行为） |
| `--param2-start/-stop/-step` | — | 第二维网格（须与 `--param2-name` 同时给出；二维时每成本档追加 sharpe/pnl 矩阵 sheet + 热力图 PNG 到 `<out>/heatmaps/`） |
| `--workers` | 0 | 并行 worker 进程数（0=自动 cpu−1，1=串行确定性路径）；spawn 池每 worker 预加载一次行情 |
| `--cost-levels` | 1,2,3 | 成本倍数 |
| `--capital` | 5000 | 年化（CAGR）与 return_pct 的分母 |
| `--label` | "" | sheet 命名与 meta |
| `--xlsx` | — | Excel 路径（追加式） |
| `--sparam k=v` | — | **策略额外参数**并入每个 run（如 `sl_pips=300`），可重复；与 `--param2-name` 同名冲突报错 |

## 5. `wfa`

| 参数（特有） | 默认 | 说明 |
|---|---|---|
| `--train-months` / `--test-months` | 24/6 | 滚动窗口（日历月，锚定每月 1 日；裁剪后 test <45 天的折丢弃） |
| `--min-train-trades` | 30 | 折内选参最低平仓笔数 |
| `--cost-levels` | 1 | 取第一个倍数（WFA 单成本档） |
| `--param2-*` / `--workers` | — | 同 sweep |
| `--sparam` | — | 同 sweep |

输出：逐折表（fold_id/train/test/chosen_p1/chosen_p2/is_*/oos_*）+ OOS 拼接聚合行 + 参数分布。

**折内选参规则（预提交，勿随结果调整）**：5×5 邻域均值 sharpe 最高的合格格**中心**（合格=自身平仓 ≥ `--min-train-trades` 且邻域有效格 ≥13/25；平票按 p1、p2 升序取首个）。选参只用**折内 train 数据**——禁止用全样本高原限制折内搜索（泄漏）。单维网格/极端稀疏时降级为合格格中自身 sharpe 最大。

## 6. 策略参数（strategies/vol_reversal.py，gitignored）

| 参数 | 默认 | 说明 |
|---|---|---|
| `param1` | 0.5 | 带宽 = H4 ATR(12) × param1，锚点=当前 H4 bar 开盘价 |
| `tp_pips` / `sl_pips` | 45 / 30 | 固定止盈/止损（品种 pip：JPY 0.01、EUR 0.0001、XAU 0.1） |
| `tp_atr_mult` / `sl_atr_mult` | —（关闭） | **V2**：可**独立**启用——给定时该侧距离随信号桶 ATR 缩放（入场时锁定），未给的侧保持固定 pips；两者都缺省 = v1 固定 TP45/SL30。sweep 的 `--param2-name tp_atr_mult` 即扫此参数 |
| `atr_period` | 12 | ATR 周期（H4 桶数；12=48H） |
| `warmup_days` | 20 | 预热天数（不交易，仅产 ATR） |

## 7. 单元测试覆盖什么（175 例，全离线不登录）

| 层 | 文件 | 测什么 |
|---|---|---|
| 交易安全（fxcm_api） | test_stops/test_market_open/test_logs/... | 止损只收紧不放宽、端点门控、订单日志——**实盘资金安全** |
| 引擎正确性 | test_quant_engine | 带沿成交/跳空按开盘/SL 优先/逐笔汇率折算——与手算对账 |
| 策略数学 | test_quant_vol_strategy（本地不入库） | ATR 严格先桶（零前视锁）、双带镜像、预热 NaN、端到端 PnL 对账 |
| 数据层 | test_quant_data/test_store | 重采样口径、PreloadedFeed 切片、WAL 读写 |
| 扫参/WFA | test_quant_validate | 折切分边界（手算）、OOS 拼接、聚合行 |
| 回填 | test_backfill_official | 官方周 CSV 解析、404 语义 |

即：**两部分**——实盘交易安全 + 回测引擎数值正确性（关键语义都有"手算对账"型用例锁定）。

## 8. 已知简化

swap 未建模（平均持仓 13-14h，2022 后利于多头）；无保证金强平建模（XAU 大仓位情景失真）；主库 XAU m1 分钟覆盖 ~40%；官方源为指示性报价（与账户实价有基差）。
