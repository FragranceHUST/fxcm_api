# 波动率动态止损方案（v1 草案，待批复）

> 目标：把 guard 的固定点数止损（INIT/BE/TRAIL 全链路）升级为**按品种波动率自适应**，
> 落实项目最初的路线图——"根据波动率动态定义止损额度，随盈利增长不断上调止损位"。
> 原则：**纯函数层（stops.py）零改动**；波动率只改变喂给级联的 pips 参数。

## 0. 现状与接入点

```
StopManager.run_cycle()                        ← 唯一接线点（500ms 轮询）
  └─ evaluate_trade(trade, offer, pip,
         initial_sl_pips=20, be_trigger_pips=50, ...)   ← 目前全是静态配置值
```

- 级联数学（INIT→BE→TRAIL、"只改善不放宽"、贴边钳制）全部在纯函数里，**不动**
- 波动率模块负责：算出"此刻该品种的 ATR" → 把 `*_pips` 参数换成 `mult × ATR(pips)`
- daemon 内 `CandleStore` 已有 1m/15m/1h/4h 收盘K线持续落库（real 会话数据面），ATR 数据源现成

## 1. 波动率口径：Wilder ATR

- `TR_i = max(H−L, |H−C_prev|, |L−C_prev|)`；首值 = 前 period 个 TR 的 SMA，其后 Wilder 递归 `ATR_i = (ATR_prev×(period−1) + TR_i) / period`
- 取数：`store.get_candles(symbol, tf, limit=4×period+1)`（Wilder 收敛余量），收盘K线，bid 口径
- 单位换算：`atr_pips = ATR(价格单位) / pip`（pip 走现有 `pip_overrides`，XAU/USD=0.1）
- 不可用回退：K线不足 period+1 根 / 品种无数据 → **整链路回退 fixed 模式**并 WARN（每品种只告警一次）

## 2. 参数映射（mult 无量纲，随 ATR 自动缩放）

| 级联 | fixed 模式（现状） | atr 模式 | demo 建议值 | real 建议值 |
|---|---|---|---|---|
| 初始止损 | `initial_sl_pips=20/150` | `sl_mult × ATR`，再钳制 | `sl_mult=1.0`，钳 [10,100] | `sl_mult=1.5`，钳 **[150,300]**（下限=现行值：平静市不紧于现状，风暴市允许放宽至上限） |
| 保本触发 | `be_trigger_pips=50/51` | `be_trigger_mult × ATR` | `2.0`（≈50 pips @ATR 25） | `2.0`（≈50 pips，与现行 51 对齐） |
| 保本缓冲 | `be_buffer_pips=0/1` | **保持绝对 pips 不变**（滑点驱动，与波动率无关） | 不变 | 不变 |
| 移动启动 | `trail_start_pips` | `trail_start_mult × ATR` | `2.0` | `2.0` |
| 移动间距 | `trail_dist_pips` | `trail_dist_mult × ATR` | `2.5` | `2.5` |
| 移动步长 | `trail_step_pips` | **保持绝对 pips**（防抖，与波动率无关） | 不变 | 不变 |
| 最小距离 | `min_stop_distance_pips` | 不变（安全底座） | 不变 | 不变 |

> use_trailing 仍为总开关，默认关——波动率只改变参数值，不改变级联结构。

## 3. 关键设计决策

**3.1 ATR 快照（快照式，非实时跟随）**
每笔持仓**首次被 guard 看到时**冻结一次 ATR（`{trade_id: atr_pips}` 内存缓存），INIT/BE/TRAIL 全程用该快照：
- 语义确定：同一持仓的止损参数不随时间漂移，可复现、可回测
- 免疫"波动放大 → trail 间距变宽 → 与'只改善'规则打架"的竞态
- 持仓平仓后清理缓存（每周期顺带 prune，成本可忽略）
- 代价：波动骤降时 trail 间距偏宽——v1 接受，列为后续可选"实时 ATR"迭代

**3.2 钳制边界（新闻脉冲防护）**
ATR 在非农/FOMC 等行情会瞬间膨胀 3-5 倍，`sl_min_pips/sl_max_pips` 双向钳制初始止损；
real 下限取现行 150 pips = **波动率模式在平静市不比现状更激进**，只有风暴市才会放宽（封顶 300）。

**3.3 配置形态（向后兼容）**
```jsonc
"guard": {
  "sl_mode": "fixed",          // fixed（默认=现行为，零风险开关）| atr
  "atr": {                     // 仅 sl_mode=atr 时生效
    "tf": "15m",               // ATR 来源周期：1m/15m/1h（须已在回填/落库范围）
    "period": 14,
    "sl_mult": 1.0,
    "be_trigger_mult": 2.0,
    "trail_start_mult": 2.0,
    "trail_dist_mult": 2.5,
    "sl_min_pips": 10.0,       // 初始止损钳制下限
    "sl_max_pips": 100.0       // 初始止损钳制上限
  },
  ...其余 fixed 字段原样保留（fixed 模式用 + atr 回退用）
}
```

**3.4 生效范围**
- **daemon-only**：ATR 依赖 `CandleStore`（daemon 进程内常驻）。CLI 直连模式的 `sl` 手动改损不涉及（本来就传显式价格）；废弃中的 CLI guard 不支持 atr 模式
- ATR 缓存按 (symbol) 维护，仅当 `store.latest_ts(symbol, tf)` 前进时重算——500ms 轮询零查询压力
- 日志：启动时打印 ATR 与推导出的各级 pips；ATR 变化 ≥10% 时打印一行，dry_run 下可先并排观察

## 4. 实施步骤（每步单独提交）

| # | 内容 | 交付物 |
|---|---|---|
| 1 | `fxcm_api/data/atr.py`：`compute_atr()` 纯函数 + `AtrProvider`（缓存/失效/回退） | ~90 行 + 10 例测试 |
| 2 | `config.py`：`AtrSettings` + `sl_mode` 校验（非法值/非法 mult 直接拒载） | +8 例测试 |
| 3 | `stop_manager.py`：`_dynamic_pips()`（快照/钳制/回退）接入 `run_cycle`；平仓 prune | +8 例测试 |
| 4 | `daemon.py` 接线（按环境构建 AtrProvider）+ `config.example.json` + README | 集成自检 |
| 5 | demo `dry_run=true` 观察 24h，确认 INIT/BE 动作与 fixed 模式预期差异后再关 dry_run | 观察结论 |

## 5. 决策点（请批复）

| # | 决策点 | 建议 |
|---|---|---|
| **D1** | ATR 来源周期 | **15m / period 14**（水平稳定、抗单根脉冲；1m 反应快但抖动大）。你若偏好更敏捷可改 1m |
| **D2** | 快照 vs 实时 ATR | **快照**（开仓时冻结）。实时模式留待 v2 |
| **D3** | real 钳制边界 | **[150, 300] pips**（下限=现行值，只放宽不收紧）；demo [10, 100] |
| **D4** | BE 缓冲/移动步长 | **保持绝对 pips**，不随 ATR 缩放（滑点/防抖属性） |
| **D5** | per-symbol mult 覆盖 | **v1 不做**（real 已用 `symbol_filter` 隔离为 XAU 专属；全局 mult + 各环境配置已够） |
| **D6** | 默认 `sl_mode` | **fixed**（配置显式切 atr；demo 先行，real 观察后切） |
