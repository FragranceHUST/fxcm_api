# 量化回测系统设计方案 v1（待批复）

> 目标：在现有 fxcm_api 基础设施（SQLite K线库 / CostFunction 统计 / Web 面板）之上，
> 构建参数化回测引擎 + 验证方法栈，支撑波动率类策略的大规模参数校验与过拟合防御。
> 策略规格本身不入库（见 .gitignore 的 strategies/，规格在会话中批复）。

## 0. 已批复决策（承自前会话 D1-D12，本方案遵守）

| 编号 | 决策 |
|---|---|
| D1 | Trade/Strategy 口径按 C++ 参考实现（Fx_Quant_System/BackTest：方向=Ask/Bid，字段集对齐） |
| D2 | 成交语义=触价制（价格触及即成交，不等待收盘确认） |
| D4 | v1 单向持仓（long-only / short-only 可配），双边留待 v2 |
| D7 | 并发=CPU 多进程先行（Mac 多核），GPU 为后续独立阶段 |
| D8 | 目标函数：sharpe 为指标之一，**胜率优先**（初期追求高胜率） |
| D9 | 回测数据为十年目标（受服务端 m1 深度约束，见 §2.3） |
| D10 | 策略基础参数值由实现方给出默认 |
| 补充1 | 策略实现与规格**不入 git**（.gitignore），仅框架代码入库 |
| 需求10 | 子任务一律阻塞等待，不用异步回调 |
| 批复 0910 | 回测主区间=**过去 3 年**（2023-09→今）；数据完整验证通过前不启动回测开发（M0 门槛） |

## 1. 架构总览

```
quant/                          # 回测框架包（入库）
  data.py        # DataFeed：CandleStore 读取封装 + 1m→H4 重采样 + 完整性检查
  trade.py       # Trade 基类（C++ 口径 + 扩展字段）
  strategy.py    # StrategyBase 抽象基类（参数/品种/方向/资金/损失函数）
  engine.py      # 撮合引擎：向量化预计算 + 触价制状态机 + 盘中细化
  cost.py        # 成本模型：点差+滑点+佣金，支持压力倍数
  optimize.py    # 参数网格扫描（multiprocessing）+ 全量试验落盘
  validate.py    # WFA 切分 / 参数平原 / DSR / 置换检验
  report.py      # 结果序列化（SQLite results 表 + JSON 摘要）
  web.py         # 回测标签页 API（只读结果 + 触发入口按 B7 批复）
  cli.py         # python -m quant.cli <backtest|sweep|validate|report>
strategies/                     # ★ gitignore —— 策略实现区（不入库）
  STRATEGY_SPEC.md               # 策略规格（会话批复后落此）
  vol_reversal.py                # 波动率反转策略
static/index.html               # +回测子标签页
tests/test_quant_*.py           # 引擎/数据/验证单测（不含策略逻辑）
docs/BACKTEST_PLAN.md           # 本文档
```

依赖：numpy 1.26.4 / pandas 1.5.3 已就绪（零新增即可跑）；可选 numba 见 B1。

## 2. 数据层

### 2.1 复用
- 读取：`CandleStore.get_candles()`（注意默认 limit=5000，DataFeed 提供分页/全量迭代封装）
- 重采样：`candles.rebuild()` 的 epoch-bucket 口径与实盘层一致，1m→任意 TF 用它而非 pandas resample
- 评估：回测产出合成 `closed_trades` + `equity_curve` 直接喂现有 `compute_stats()`（14 指标，与实盘 /stats 同口径）
- 滑点经验值：`order_journal` 的 requested vs filled（实盘真实数据）

### 2.2 当前数据深度（2026-09-10 快照）
| 品种 | 1m | 1h | 4h | 1d |
|---|---|---|---|---|
| XAU/USD | ~471k 根，2023-06→今（回填仍在向更深推进） | 2016→今 | 2016→今 | 2016→今 |
| USD/JPY | ~523k 根，2022-08→今 | 2016→今 | 2016→今 | 缺（阶段3补） |
| EUR/USD | 几乎为空（阶段2a 进行中） | 2016→今 | 2016→今 | 缺（阶段3补） |

### 2.3 现实约束与对策
- m1 十年目标受服务端深度限制：XAU m1 实测约 3.2 年+（仍在加深）。触价制精度依赖 m1。
- 对策（B10）：v1 主回测区间用 m1 全深度（触价精确，~3 年+）；同时提供 10 年 H4/1h 级别的粗粒度长周期验证（触价降级到对应 TF 的 high/low）。两档并行输出。

## 3. 引擎设计

### 3.1 两段式：向量化预计算 + 状态机撮合
1. **预计算（numpy 向量化）**：指标/通道/信号数组一次性算好（H4 波动率通道、m1 穿越信号）；参数化部分只重算依赖参数的数组。
2. **撮合状态机**：逐 m1 bar 推进（D2 触价制）：
   - 挂单期：band 触价→开仓（成交价规则见 B3）
   - 持仓期：m1 high/low 触及 TP/SL 即平（D2）；同根 bar 双触按 B2 裁决
   - 单向单持仓（D4），信号在持仓期被忽略
3. **批量路径**：引擎对"无持仓 bar 段"用 numpy 跳跃（向量化定位下一个触价事件），把纯 Python 循环压缩到持仓/信号事件附近——预期单组合 10 年 m1 在秒级。

### 3.2 Trade 基类（C++ 口径对齐 + 扩展）
字段：uuid, instrument, entry_price, exit_price, stoploss_price, profit_target_price,
commission_rate, commission, profit, quantity, entry_time, exit_time,
direction(Ask/Bid), real, closed
扩展：exit_reason(tp/sl/signal/end_of_data), mfe, mae, param_set_id（试验追踪用）

### 3.3 Strategy 基类（对齐 C++ Strategy.h）
属性：type, instrument, params(dict), direction_mode(long/short/both, v1 用前两个),
total_capital, per_trade_lots, rf_rate, cost_model
方法：`run_backtest(feed, start, end)`, `run_realtime(...)`（接 daemon hub 的接口位）,
`calc_cost_function() -> compute_stats 结果`, `output(path|web)`

### 3.4 成本模型
- 点差：v1 用固定假设（XAU 往返 $0.30-0.50）+ **压力测试 ×2/×3**；
  经验校准升级：daemon 长期在线后用 OFFERS 实测点差分布 + journal 实盘滑点回填（v2）
- 佣金：XAU/USD 现配置为 0，字段保留
- 每 Trade 的 profit 扣减成本后再进 compute_stats

## 4. 并发回测（D7：CPU 多进程）

- `multiprocessing.Pool`，worker 数 = 物理核数-2（留核心给 daemon）
- 任务粒度：单 (param_set × WFA fold)；进程内只读共享 SQLite（WAL），结果经队列回写主进程
- 参数集序列化：numpy memmap 共享只读行情数组，避免每 worker 复制（10 年 m1 ≈ 数百 MB）
- GPU（CUDA/Triton/Metal）为独立后续阶段：接口已按"参数集→收益序列"批处理形态设计，届时可平移

## 5. 验证方法栈（过拟合防御）

1. **Walk-Forward（滚动窗口）**：train 70% / test 30% 滑动，按 test 长度步进重优化；
   默认 train 2 年 / test 6 个月（数据 3.2 年 → ~5 折；m1 若达 10 年则 16 折）
2. **参数平原**：全网格输出热力图，选**平坦近优区中心**而非孤立峰值；±1 步邻域稳定性检查
3. **Deflated Sharpe**：记录全部试验（含失败/中途放弃），按试验数 N 与跨试验 sharpe 方差计算 DSR，DSR>0.95 才认为显著
4. **蒙特卡洛置换**：打乱入场时点 ≥1000 次，策略须显著优于随机时点
5. **成本压力**：全流程在 1×/2×/3× 成本下重跑，结论以 3× 成本下仍存活为准
6. **多测试纪律**：每次扫描自动登记试验清单（参数→OOS 指标），人肉试验同样补录

## 6. Web 集成（需求 1）

- 新增"回测"子标签页：结果列表（试验库）、参数热力图、净值/回撤曲线、Trade 明细表、WFA 折叠 OOS 汇总
- daemon 只做**只读**结果查询（从 results 表）；回测进程独立运行（B7），不与交易会话抢 CPU
- 复用现有 Lightweight Charts 渲染净值曲线

## 7. 里程碑

| 阶段 | 交付 | 验收 |
|---|---|---|
| **M0 数据完备（前置门槛）** | XAU m1 深度续挖至服务端底 + 全品种 D1 + 顶部补齐；**数据验证脚本**（缺口审计：周末外缺失 bar 清单、覆盖率、TF 间一致性交叉核对） | 验证报告全绿后才进入 M1 |
| M1 引擎基座 | data/trade/strategy/engine + 单组合回测 CLI + 单测 | 触价制撮合与手算一致；5 年 m1 单跑 < 5s |
| M2 并发扫描 | optimize.py 多进程网格 + 结果落库 | 1000 组合全扫完成且进程稳定 |
| M3 验证栈 | validate.py（WFA/平原/DSR/置换/成本压力） | 报告输出"可信参数区"而非单点 |
| M4 Web 标签页 | 只读结果页 | 浏览器可查任意试验明细 |
| M5 策略首轮 | vol_reversal（不入库）+ 全网格 + 首份分析报告 | 含 DSR 与成本压力结论 |

## 8. 待批复决策点（B1-B10）

| 编号 | 问题 | 建议 |
|---|---|---|
| B1 | 引擎运行时：纯 numpy（零新依赖）vs 引入 numba（C 级循环，pip 新依赖） | 引入 numba（扫参提速 5-20×，Mac 原生支持） |
| B2 | 同根 m1 内 TP/SL 双触裁决 | 保守：SL 优先（行业标准，不美化结果） |
| B3 | 触价成交价：按 band/TP/SL 理想价成交 vs 按穿越那根 m1 的 open 成交 | 理想价+半点差成本（触价制语义，保守性由成本压力补足） |
| B4 | 波动率定义：48h 窗口 max(High)−min(Low)（Donchian 口径） vs 平均每根 H4 (H−L)（ATR 口径，与 VOLATILITY_STOP_PLAN 一致） | v1 用 Donchian（更贴近你"区间"表述），ATR 作对照参数 |
| B5 | 扫参策略：全网格（平原分析需要完整地形） vs Optuna 贝叶斯 | 全网格，上限 ~5000 组合（p1×p2×p3） |
| B6 | 目标函数：胜率字典序（winrate≥阈值内最大化 sharpe，阈值=全网格 60 分位） vs 加权合成 | 字典序（符合 D8 且避免权重拍脑袋） |
| B7 | 回测入口：CLI 独立进程 + Web 只读 vs Web 可触发子进程 | v1 CLI+只读（保护交易 daemon 性能），触发按钮 M5 后再说 |
| B8 | 代码布局：顶层 quant/ 包 vs fxcm_api/quant/ 子包 | 顶层 quant/（与 daemon 解耦，导入 fxcm_api.data 复用） |
| B9 | 验证栈 v1 范围：WFA+平原+成本压力先行 vs 一次做全（+DSR+置换） | 先行三项，DSR/置换在首轮全网格结果出来后立即补 |
| B10 | 回测主区间：按批复定为**过去 3 年**（2023-09→今）；XAU m1 当前深度 2023-06-04 起，已覆盖 3 年目标，回填继续向服务端真实底推进以留余量；10 年 H4 粗粒度档保留作长周期稳健性对照 | 双档并行（主结论用 m1 档，长周期用 H4 档做稳健性对照） |
