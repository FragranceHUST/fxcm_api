# ForexConnect API 参考手册（Python / forexconnect 1.6.43）

> 面向 OrderGuard-Python 项目整理，基于**本机安装包内省**（`scripts/dump_api_reference.py` 可随时重新生成原始快照 `docs/forexconnect_api_dump.md`）+ **官方文档逐字段核验**（fxcm/ForexConnectAPI `help/Python/` @ commit `4881fa71ee52`，204 个成员页面已解析）+ 官方样本 + 你的 real 账户实测。
>
> 证据标注：✅ 实测（你的账户输出验证）｜📖 官方文档核验｜⚙️ 内省（100% 来自你安装的包）

---

## 1. 快速定位

| 你想做什么 | 用什么 |
|---|---|
| 登录一次，长期持有会话 | `ForexConnect().login(...)` + 会话保活（见 §3） |
| 实时看报价 | 轮询 `get_table(OFFERS)` 或订阅 `table.subscribe_update(...)`（见 §5） |
| 看持仓/账户/挂单 | `get_table(TRADES / ACCOUNTS / ORDERS / ...)`（见 §4） |
| 下单/改单/删单 | `create_order_request(...)` + `send_request(...)`（见 §6） |
| 查最小止损距离等交易规则 | `login_rules.trading_settings_provider`（见 §7） |
| 拉历史 K 线（回测/研究） | `get_history(...)` 或 `PriceHistoryCommunicator`（见 §8） |

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│ ForexConnect（高层封装，我们主要用这层）                     │
│   login / logout / get_table / create_order_request      │
│   send_request / get_history / login_rules               │
├─────────────────────────────────────────────────────────┤
│ O2GSession（会话）           O2GTableManager（表格管理器）  │
│   状态机/订阅/请求工厂          维护 7 张表的实时副本          │
├─────────────────────────────────────────────────────────┤
│ fxcorepy（C++ 核心，Boost.Python 绑定）                    │
│   TCP/TLS 连接 FXCM 服务器（www.fxcorporate.com/Hosts.jsp）│
└─────────────────────────────────────────────────────────┘
```

两条数据通道：

- **表格通道**（table manager）：登录后自动加载并在内存维护 7 张表，服务器有变化自动推送更新行——**读表即读最新值**，这就是"登录一次、长期复用"的基础
- **请求通道**（request factory）：下单/改单走 `create_order_request → send_request`，返回响应（订单 ID、错误信息）

---

## 3. 登录与会话生命周期

### 3.1 登录（✅ 已实测）

```python
from forexconnect import ForexConnect

fx = ForexConnect()
fx.login(
    user_id="...", password="...",
    url="www.fxcorporate.com/Hosts.jsp",
    connection="real",            # "demo" / "real"
    session_id=None,              # 多数据库用户才需要
    pin=None,                     # 有 PIN 的用户才需要
    session_status_callback=cb,   # (session, status) -> None，可选
    use_table_manager=True,       # 必须 True：启用表格自动维护
)
```

`login()` 阻塞直至连上或失败；返回后 `fx.get_table(...)` 即可用。

### 3.2 会话状态机（官方语义）⚙️📖

`AO2GSessionStatus` 共 11 个状态（官方文章：[SessionStatusPy](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/SessionStatusPy.html)）：

| 状态 | 官方语义 | 流转 |
|---|---|---|
| `CONNECTING` | 登录信息已提交，连接中 | → CONNECTED / TRADING_SESSION_REQUESTED / DISCONNECTED（登录失败） |
| `TRADING_SESSION_REQUESTED` | 多数据库或需 PIN；登录挂起，等待 `set_trading_session` 提交 | → CONNECTED / DISCONNECTED |
| `CONNECTED` | ✅ 登录成功。**绝大多数会话方法仅在此状态可用** | → DISCONNECTING / RECONNECTING / PRICE_SESSION_RECONNECTING / SESSION_LOST |
| `RECONNECTING` | 交易连接断开，**API 自动重试，最多 20 次**；成功则回 CONNECTED | → CONNECTED / SESSION_LOST |
| `PRICE_SESSION_RECONNECTING` | 仅行情通道断线重连 | — |
| `CHART_SESSION_RECONNECTING` | 仅图表通道重连 | — |
| `SESSION_LOST` | 重连 20 次耗尽 / 会话过期 / 服务器关闭。**必须手动重新 login** | → CONNECTING（自行重连） |
| `DISCONNECTING` → `DISCONNECTED` | 主动登出中 / 完成。官方提示：Linux 下须等 DISCONNECTED 后再释放会话 | — |
| `CONNECTED_WITH_NEED_TO_CHANGE_PASSWORD` | 已连接但要求改密 | — |
| `UNKNOWN` | 未识别的服务器状态 | — |

**长驻进程要点**：`RECONNECTING` 期间底层自动重试（上限 20 次），**不要**在此时手动 login；收到 `SESSION_LOST` 才重新 `login()`。辅助类 `forexconnect.SessionStatusListener`（含 `connected/disconnected/last_error`、`wait_events()`）由 `ForexConnect.login()` 自动创建；也可通过 `fx.session.subscribe_session_status(...)` 自行监听。

### 3.3 会话复用（回答你的问题 2）✅

- `login()` 一次后，只要进程不退出、会话不掉线，**所有读操作都是复用同一会话**——我们 `stream`/`guard` 命令就是这种模式
- 表格由 table manager 持续自动刷新，**每次读 `get_table(...)` 拿到的都是最新值**
- 底层会话还提供 `token`（`fx.session.token`）——可用于下次 `login_with_token`，避免重复传密码
- 断线防护：`RECONNECTING` 自动重连；进程级健壮性（掉线重 login）是我们后续要加的包装层

### 3.4 表格管理器状态 ⚙️📖

两级状态要区分清楚：

- **全局** `O2GTableManagerStatus`：`TABLES_LOADING`（加载中）→ `TABLES_LOADED`（就绪，**唯一可用状态**）｜`TABLES_LOAD_FAILED`（失败）
- **单表** `O2GTableStatus`：`INITIAL` / `REFRESHING` / `REFRESHED`（可安全读取）/ `FAILED`

`session.use_table_manager(mode, listener)` 必须在 login **之前**调用；我们的封装 `ForexConnect.login(use_table_manager=True)` 会自动设置并**阻塞等待 `TABLES_LOADED`** 后才返回（未就绪则抛 "Table manager not ready"——见 PyPI 包 `forexconnect/ForexConnect.py` 内部实现）。因此 `login()` 正常返回即代表 7 张表已就绪可读。

---

## 4. 表格体系（7 张表）⚙️

通过 `fx.get_table(ForexConnect.XXX)` 获取，类型常量：`ACCOUNTS / TRADES / OFFERS / ORDERS / CLOSED_TRADES / SUMMARY / MESSAGES`。

### 行对象通用 API（⚙️ 内省）

| 成员 | 说明 |
|---|---|
| `row.columns` | 列集合，元素为 `O2GTableColumn`（属性：`.id` 列名、`.is_key`、`.type`） |
| `row.get_cell(col)` | 按列取值 |
| `row.is_cell_changed(col)` | 本次更新中该列是否变化 |

### 表格对象 API（⚙️ 内省）

| 成员 | 说明 |
|---|---|
| `table.size` / `len(table)` | 行数 |
| `table.get_row(i)` / `table[i]` / 迭代 | 逐行访问 |
| `table.get_rows_by_column_value(col, val)` | 按列值过滤 |
| `table.get_rows_by_condition(...)` / `get_rows_by_multi_column_values(...)` | 条件查询 |
| `table.for_each_row(fn)` | 遍历回调 |
| `table.subscribe_update(...)` / `unsubscribe_update(...)` | **事件推送订阅**（见 §5） |
| `table.status` / `subscribe_status(...)` | 表格状态 |

### TRADES（当前持仓）——止损管家的主战场 📖

官方全字段（[O2GTradeTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GTradeTableRow.html)，共 25 个数据字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `trade_id` | str | 持仓 ID（EDIT_ORDER 的 TRADE_ID 参数） |
| `offer_id` / `account_id` | str | 品种 / 账户关联 ID |
| `account_name` / `account_kind` | str | 账户名 / 账户类型 |
| `symbol`（文档为 instrument 语义） | str | 品种名（如 EUR/USD） |
| **`buy_sell`** | str | **方向（'B'/'S'）——TRADES 行没有 is_buy 属性，误用会 AttributeError** |
| `amount` | int | 手数（单位 = base unit size） |
| `open_rate` / `open_time` | float / datetime | 开仓价 / 时间 |
| `open_order_id` / `open_order_req_id` / `open_order_request_txt` / `open_quote_id` | str | 开仓订单 / 请求 / 自定义备注 / 报价对 ID |
| `parties` | str | 下单环境标识 |
| **`stop`** | float | **当前止损价（0 = 无）** |
| **`stop_order_id`** | str | **止损单 ID（空 = 无，决定 EDIT 还是 CREATE）** |
| `limit` / `limit_order_id` | float / str | 止盈同构字段 |
| `close` | float | 当前可平仓价（买仓 = bid） |
| `gross_pl` | float | 浮动盈亏（账户币种） |
| `pl` | float | 单手盈亏 |
| `commission` / `rollover_interest` / `dividends` | float | 佣金 / 隔夜利息 / 股息（仅指数 CFD） |
| `used_margin` | float | 占用保证金 |
| `trade_id_origin` | str | 部分平仓来源的持仓 ID |
| `value_date` | str | 交割日 |

> 注：你账户实际暴露的列以 `describe` 输出为准（不同账户实体字段略有差异）。**demo 实测：新账户 TRADES 为空；行字段在开仓后用 `positions` 命令验证**。

### OFFERS（报价）📖✅

官方全字段（[O2GOfferTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GOfferTableRow.html)）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `offer_id` | str | 品种 ID |
| `instrument` | str | 品种名（✅ 实测：EUR/USD、USD/JPY） |
| `bid` / `ask` | float | 买/卖价（✅ 实测） |
| `point_size` / `digits` | float / int | 最小报价单位 / 小数位（✅ 实测：USD/JPY point=0.01, digits=3） |
| `fractional_pip_size` | float | 分数 pip 大小（3/5 位报价时 = 10×point） |
| `time` | datetime | 报价时间 |
| `volume` | int | 成交量 |
| `high` / `low` | float | 当日高/低 |
| `bid_tradable` / `ask_tradable` | str | 该侧是否可成交 |
| `subscription_status` | str | T 可交易 / V 仅看 / D 禁用 |
| `trading_status` | str | 开市/闭市状态 |
| `contract_currency` / `contract_multiplier` | str / float | 合约币种 / 乘数 |
| `buy_interest` / `sell_interest` | float | 多/空隔夜利息 |
| `pip_cost` | float | 每 pip 价值 |
| `quote_id` / `value_date` | str | 报价对 ID / 交割日 |
| `instrument_type` | 枚举 | 品种类型 |
| `ask_expire_date` | datetime | ask 有效期（内部用途） |

> 另有 `is_bid_valid()` / `is_ask_valid()` 等 20 个有效性校验方法（⚙️ 内省确认）。
>
> ✅ demo 实测补充字段（517 个品种）：`bid_id`/`ask_id`、`dividend_buy`/`dividend_sell`、`bid/ask/hi/low_change_direction`、`default_sort_order`。`pip_cost`（每 pip 价值）确认存在——实现"固定金额止损"模式时的现成输入。

### ACCOUNTS（账户）📖✅

官方全字段（[O2GAccountTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GAccountTableRow.html)）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `account_id` | str | 账户 ID（下单 ACCOUNT_ID 参数）✅ 实测 |
| `balance` | float | 余额 ✅ 实测 |
| `equity` / `m2m_equity` | float | 净值 / 逐日盯市净值 |
| `usable_margin` | float | 可用保证金 |
| `non_trade_equity` | float | 非交易净值 |
| `day_pl` / `gross_pl` | float | 当日盈亏 / 总浮动盈亏 |
| `base_unit_size` | int | 基础单位（1 手 = N） |
| `amount_limit` | int | 单笔最大手数限制 |
| `leverage_profile_id` / `atpid` | str | 杠杆方案 ID / 佣金方案 ID |
| `account_name` / `account_kind` | str | 账户名 / 类型 |
| `margin_call_flag` / `maintenance_flag` / `maintenance_type` | str/bool/int | 追保 / 维护状态标记 |
| `last_margin_call_date` | datetime | 最近追保时间 |
| `manager_account_id` | str | 管理账户 ID |

> ✅ demo 实测补充（共 25 列）：`used_margin`、`used_margin3`、`hedge_margin_pct`、`arpid`、`usable_margin_in_percentage`、`usable_maint_margin_in_percentage`。命名差异：服务器列名 `m2_mequity`（官方文档写 `m2m_equity`），未使用无影响。

### ORDERS（挂单/条件单）📖

官方全字段（[O2GOrderTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GOrderTableRow.html)，42 个）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_id` / `request_id` | str | 订单 ID / 创建该订单的请求 ID |
| `account_id` / `account_name` / `account_kind` | str | 下单账户 |
| `offer_id` | str | 品种 |
| `amount` / `origin_amount` / `filled_amount` | int | 当前 / 原始 / 最近成交手数 |
| `net_quantity` | bool | Net Amount 单标记 |
| `buy_sell` | str | 方向（'B'/'S'） |
| `type` | str | 订单类型（对应 Constants.Orders） |
| `stage` | str | 订单动作（Open/Close…） |
| `status` / `status_time` | str / datetime | 状态 / 状态更新时间 |
| `rate` | float | 挂单价格 |
| `rate_min` / `rate_max` | float | 成交价上下限 |
| `at_market` | float | 距市价可接受执行范围 |
| `time_in_force` | str | GTC/IOC/FOK/DAY/GTD |
| `expire_date` | datetime | GTD 过期时间 |
| `limit` / `stop` | float | 关联止盈/止损价（表格计算值） |
| `trail_rate` / `trail_step` | float / int | 追踪价 / 追踪步长（pips） |
| `stop_trail_rate` / `stop_trail_step` | float / int | 关联追踪止损的价格/步长 |
| `execution_rate` | float | 实际成交价 |
| `lifetime` | float | 重新报价应答时限 |
| `contingency_type` / `contingent_order_id` / `primary_id` | 枚举/str/str | 条件单组（OCO/OTO/OTOCO/ELS）关联 |
| `peg_offset` / `peg_type` | float / str | 挂钩偏移 / 挂钩基准价类型 |
| `type_limit` / `type_stop` | 枚举 | 止盈/止损是固定价还是挂钩价 |
| `trade_id` | str | 该单将要开/平的持仓 |
| `value_date` | str | 交割日 |
| `working_indicator` | bool | 执行中标记 |
| `parties` / `request_txt` | str | 下单环境标识 / 自定义备注 |

### CLOSED_TRADES（历史平仓）📖

官方全字段（[O2GClosedTradeTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GClosedTradeTableRow.html)，32 个）：`trade_id`、`account_id/name/kind`、`offer_id`、`amount`、`buy_sell`、`open_rate/open_time`、**`close_rate/close_time`**、开/关仓各自的 `order_id/req_id/request_txt/parties`（共 8 个 str）、`open_quote_id/close_quote_id`（str）、`gross_pl`、`pl`（单手）、`commission/rollover_interest/dividends`、`trade_id_origin`（部分平仓来源）、`trade_id_remain`（部分平仓产出的新持仓）、`value_date`。

> ⚠️ 官方文档生成缺陷：`getOpenOrderID`/`getOpenQuoteID` 成员页的 Python 声明误写为 datetime/float，按语义与 C++ API 均为 str。

### SUMMARY（品种汇总）📖

15 字段（[O2GSummaryTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GSummaryTableRow.html)）：`instrument`、`offer_id`、`amount`、`buy_amount`、`sell_amount`、`buy_avg_open`、`sell_avg_open`、`buy_close`、`sell_close`、`buy_net_pl`、`sell_net_pl`、`net_pl`、`gross_pl`、`dividends`、`default_sort_order`。（该表行无 `columns`/`table_type` 属性）

### MESSAGES（服务器消息）📖

9 字段（[O2GMessageTableRow](https://github.com/fxcm/ForexConnectAPI/blob/4881fa71ee52/help/Python/IO2GMessageTableRow.html)）：`msg_id`、`time`、`type`（MessageType 枚举：REGULAR/POPUP/FORCED_POPUP/ANSWER）、`feature`（MessageFeature：INFORMATION/TRADING_HOURS/QUESTION/MARKET_CONDITION/SOFTWARE_UPDATE/EMERGENCY/SYSTEM_FAILURE/PLAIN）、`from`、`subject`、`text`、`html_fragment_flag`。**下单被拒的原因会出现在这里**——guard 后续应订阅此表捕获拒单反馈。

---

## 5. 行情的两种订阅模式（回答你的问题 2）

### 模式 A：轮询（当前 OrderGuard 采用）✅

```python
while True:
    for row in fx.get_table(ForexConnect.OFFERS):
        ...  # 每次读取都是 table manager 维护的最新值
    time.sleep(0.5)
```

- 优点：简单、可控、天然兼容"每 500ms 评估一轮"的策略节奏
- 缺点：最长有一个轮询周期的感知延迟

### 模式 B：事件推送（更低延迟，预留升级）

```python
from forexconnect import fxcorepy

class Listener(fxcorepy.AO2GTableListener):
    def on_added(self, row_id, row): ...
    def on_changed(self, row_id, row): ...      # 报价每次跳动触发
    def on_deleted(self, row_id, row): ...
    def on_status_changed(self, status): ...

offers = fx.get_table(ForexConnect.OFFERS)
offers.subscribe_update(listener)               # 取消: unsubscribe_update(listener)
```

⚙️ 事件类型 `O2GTableUpdateType`：`INSERT / UPDATE / DELETE / UPDATE_UNKNOWN`。

**建议**：guard 策略层用模式 A 就够（MQL4 版同节奏）；若后续要"tick 级"推保本反应，对目标品种的 OFFERS 行用模式 B，收到 `on_changed` 后立刻评估该品种持仓——两者可共存。

---

## 6. 下单请求系统

### 6.1 create_order_request ⚙️📖

```python
request = fx.create_order_request(
    order_type=fxcorepy.Constants.Orders.STOP,        # 订单类型（见 6.3）
    command=fxcorepy.Constants.Commands.EDIT_ORDER,   # 命令（见 6.2）
    **kwargs,                                          # 参数键值对（见 6.4）
)
response = fx.send_request(request)   # 同步等待；另有 send_request_async
```

**参数类型分发**（📖 源码验证，重要）：kwargs 值按 Python 类型自动调用对应 setter——

| Python 类型 | 底层调用 |
|---|---|
| `str` | `set_string` |
| `int` | `set_int` |
| `bool` | `set_bool` |
| `float` | `set_double` |

所以 **RATE 传 float、AMOUNT 传 int** 是唯一正确用法（传 str 会走 set_string 导致下单参数错误）。

### 6.2 Commands 命令 ⚙️

常用：`CREATE_ORDER`（新建，含给持仓挂止损）、`EDIT_ORDER`（修改已有单）、`DELETE_ORDER`（删单）、`JOIN_TO_NEW_CONTINGENCY_GROUP` / `CREATE_OCO` / `CREATE_OTO` / `CREATE_OTOCO`（条件单组）、`ACCEPT_ORDER`（执行挂单）、`CHANGE_PASSWORD`、`SEND_MAIL` 等，共 17 个。

### 6.3 Orders 订单类型 ⚙️

| 类别 | 类型（值） | 用途 |
|---|---|---|
| 市价 | `MARKET_OPEN`（'O'）/ `TRUE_MARKET_OPEN`（'OM'） | 开仓；TRUE_* 为新版市价（支持更大流动性场景） |
| 平仓 | `MARKET_CLOSE`（'C'）/ `TRUE_MARKET_CLOSE`（'CM'） | 按持仓平仓 |
| 止损/止盈 | `STOP`（'S'）/ `LIMIT`（'L'）/ `CLOSE_LIMIT`（'CL'） | **我们给持仓挂的止损就是 `STOP`** |
| 挂单 | `ENTRY`（'E'）/ `STOP_ENTRY`（'SE'）/ `LIMIT_ENTRY`（'LE'） | 预挂开仓单 |
| 追踪 | `STOP_TRAILING_ENTRY`（'STE'）等 Trailing 系列 | 追踪类挂单 |
| 区间 | `MARKET_OPEN_RANGE`（'OR'）等 Range 系列 | 区间市价/区间挂单 |
| 空头限价 | `OPEN_LIMIT`（'OL'） | 空头限价单 |

### 6.4 kwargs 参数键（O2GRequestParamsEnum 全集节选）⚙️

高频：`ACCOUNT_ID`、`OFFER_ID`、`SYMBOL`、`BUY_SELL`（'B'/'S'，常量 `Constants.BUY/SELL`）、`AMOUNT`、`RATE`（限价/触发价）、`TRADE_ID`（关联持仓）、`ORDER_ID`（EDIT/DELETE 时指定目标单）、`TIME_IN_FORCE`、`CUSTOM_ID`（自定义备注）。

挂单/条件单相关：`RATE_STOP`、`RATE_LIMIT`（开仓单同请求附止损止盈）、`PEG_TYPE` / `PEG_OFFSET`（及 `_STOP/_LIMIT` 变体，追踪挂单用）、`TRAIL_STEP`（追踪步长）、`EXPIRE_DATE_TIME`（GTD 过期）。

> 全部 ~90 个键见 `docs/forexconnect_api_dump.md` 的 O2GRequestParamsEnum 章节。

### 6.5 时效（TIF）⚙️

`GTC`（撤销前有效）、`DAY`（当日）、`IOC`（立即成交否则取消）、`FOK`（全部成交否则取消）、`GTD`（指定日失效）。

### 6.6 响应 ⚙️📖

`send_request` 返回响应对象；`fx.create_reader(...)` / `get_table_reader(...)` / `response_listener` 用于解析响应与异步回调。订单提交类请求可通过 `O2GOrderResponseReader` 拿到 `order_id`；错误时 `request_factory.last_error` 给出原因。

---

## 7. 交易规则查询（O2GTradingSettingsProvider）⚙️

入口：`provider = fx.login_rules.trading_settings_provider`

| 方法 | 说明 |
|---|---|
| `get_cond_dist_stop_for_trade(offer_id)` | **持仓止损单的最小条件距离**——服务端允许的止损距市价最近值 |
| `get_cond_dist_limit_for_trade(offer_id)` | 持仓止盈单最小距离 |
| `get_cond_dist_entry_stop/limit(offer_id)` | 挂单（entry）最小距离 |
| `get_min_quantity(offer_id)` / `get_max_quantity(offer_id)` | 最小/最大手数 |
| `get_base_unit_size(offer_id)` | 基础单位（1 手 = N，决定 amount 数量级） |
| `get_mmr(offer_id)` | 该品种保证金要求 |
| `get_margins(offer_id)` | 分层保证金 |
| `get_market_status(offer_id)` | 开/闭市状态 |
| `min_trailing_step` / `max_trailing_step` | 追踪步长允许范围 |

> 💡 **重要**：我们当前 `min_stop_distance_pips=0.5` 是手填的安全估计；正确姿势是每周期用 `get_cond_dist_stop_for_trade(offer_id)` 查实时值做 clamp（系统属性里还有全局 `COND_DIST`）。这是 guard 的下一个升级点。

---

## 8. 历史行情与回测支撑 ⚙️

- `fx.get_history(instrument, timeframe, "yyyy-MM-dd", "yyyy-MM-dd", max_bars)`：按品种+时间框架+区间拉 K 线/tick
- 时间框架：`fx.get_timeframe("m1"/"H1"/"D1"...)`、`parse_timeframe`、`TimeframeFactory`
- `PriceHistoryCommunicator`（+`Factory/Request/Response/Error`）：独立的历史价格管理器，适合批量下载与本地缓存（回测数据管道）
- K 线开盘价模式：`O2GCandleOpenPriceMode`
- 服务器时间：`fx.session.server_time`、`O2GTimeConverter`（UTC 换算，回测对时用）
- 免费历史 CSV（无需登录）：`candledata.fxcorporate.com`（详见 fxcm/MarketData 仓库）

---

## 9. 其他能力（了解即可）⚙️

| 对象 | 用途 |
|---|---|
| `O2GMargins` / `get_margins` | 分层保证金明细 |
| `O2GCommissionsProvider` | 佣金规则 |
| `O2GLevel2MarketDataUpdatesReader*` | Level2 深度数据 |
| `O2GPermissionChecker` | 账户权限（能否交易某品种） |
| `O2GRolloverProvider` | 库存费/隔夜利息 |
| `get_report_url` | 服务端报表链接 |
| `O2GSystemPropertiesReader` | 系统属性（基础币种、结算时间、COND_DIST 等） |
| `trading_session_descriptors` | 多交易子会话列表（配合 session_id/PIN） |

---

## 10. 与 OrderGuard-Python 的映射

| 模块/动作 | 用到的 API |
|---|---|
| `session.py` | `ForexConnect().login(...)`（use_table_manager=True）、`logout()` |
| `stop_manager._snapshots()` | `get_table(OFFERS)` / `get_table(TRADES)` 迭代 + 行属性读取 |
| `stop_manager._apply()` | `create_order_request(STOP, EDIT_ORDER/CREATE_ORDER, RATE/TRADE_ID/ORDER_ID/BUY_SELL/AMOUNT/SYMBOL)` + `send_request` |
| `stops.py` | 纯数学，不触 API（离线可测） |
| CLI `stream` | 轮询 `get_table(OFFERS)` |
| CLI `describe` | `row.columns` → `col.id` |
| CLI `positions` | TRADES × OFFERS 联查（guard 视角快照） |
| CLI `open` / `close` | `TRUE_MARKET_OPEN` / `TRUE_MARKET_CLOSE`（验证工具箱，trading.py） |
| CLI `sl` | 手动止损（复用 `StopManager._apply`，受 dry_run 约束） |

**已排期的扩展点**：
1. `get_cond_dist_stop_for_trade` 动态最小距离（替代手填 0.5 pip）
2. `subscribe_update` 事件式 guard（tick 级反应）
3. `SESSION_LOST/DISCONNECTED` 自动重 login
4. `CLOSED_TRADES` + `get_history` 支撑回测与绩效统计
5. `dry_run`（已实现 ✅）：观察模式，只打印不发单

---

## 11. 附录

- 原始内省快照：`docs/forexconnect_api_dump.md`（由 `scripts/dump_api_reference.py` 生成，升级包后重跑）
- 官方样本：github.com/gehtsoft-usa/forex-connect `samples/Python/`（SetStop.py、OpenPosition.py、GetOffers.py 等）
- 官方 C++ 文档：apiwiki.fxcorporate.com；社区 wiki：fxcodebase.com
