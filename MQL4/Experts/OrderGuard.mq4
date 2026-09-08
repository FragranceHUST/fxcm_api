//+------------------------------------------------------------------+
//|                                                    OrderGuard.mq4 |
//|        订单管家：为账户内所有持仓自动附加并管理止损 (MT4 / MQL4)     |
//|                                                                  |
//| V1 策略：                                                          |
//|   1. 初始止损：无止损的持仓自动附加固定点数止损                      |
//|   2. 保本推损：浮盈达到触发点数后，立即将止损推至开仓价±缓冲之上      |
//|   3. 移动止损：可选（默认关闭），盈利扩大后按固定间距逐步上移         |
//|                                                                  |
//| 设计原则：                                                         |
//|   - 只管理：不开仓、不平仓，止损只向有利方向移动，绝不放宽            |
//|   - 多品种：挂任意一张图表即可管理全账户持仓，OnTick + 500ms 定时兜底 |
//|   - 合规：遵守经纪商 STOPLEVEL / FREEZELEVEL 最小距离限制            |
//|   - 已有止损的订单不会被覆盖初始值，但保本/移动逻辑仍会尝试改善它      |
//|                                                                  |
//| V2 规划：ATR 波动率自适应止损额度、渐进式移动止损                    |
//+------------------------------------------------------------------+
#property copyright "OrderGuard"
#property link      ""
#property version   "1.00"
#property strict

//=== 止损额度（单位：pip，自动适配 3/5 位报价，可用 InpPipOverrides 覆盖）===
input double InpInitialSLPips     = 20.0;  // 初始止损距离（pips，0=不自动加初始止损）
input double InpBETriggerPips     = 5.0;   // 保本触发：浮盈达到该 pips 后推保本
input double InpBEBufferPips      = 1.0;   // 保本缓冲：止损推至开仓价±该 pips（0=正好开仓价）

//=== 移动止损（V2 预留，默认关闭）========================================
input bool   InpUseTrailing       = false; // 启用固定间距移动止损
input double InpTrailStartPips    = 10.0;  // 移动止损启动浮盈（pips）
input double InpTrailDistPips     = 8.0;   // 止损与市价保持的距离（pips）
input double InpTrailStepPips     = 1.0;   // 止损至少改善该 pips 才实际发单修改

//=== 管理范围 ============================================================
input bool   InpCurrentSymbolOnly = false; // true=仅管理当前图表品种，false=全账户
input string InpPipOverrides      = "";    // pip 覆盖表，如 "XAUUSD=0.1;XAGUSD=0.01"
input bool   InpVerboseLog        = false; // 打印跳过/重试等细节日志

//=== 常量与全局 ==========================================================
#define EA_NAME           "OrderGuard"
#define MANAGE_TIMER_MS   500        // 多品种轮询周期（毫秒）
#define MODIFY_MAX_TRIES  3          // OrderModify 重试次数

// MT4 交易错误码（自带映射，避免依赖 stderror.mqh）
#define TCE_NO_RESULT        1
#define TCE_SERVER_BUSY      4
#define TCE_NO_CONNECTION    6
#define TCE_TIMEOUT          128
#define TCE_INVALID_PRICE    129
#define TCE_INVALID_STOPS    130
#define TCE_TRADE_DISABLED   133
#define TCE_REQUOTE          135
#define TCE_PRICE_CHANGED    136
#define TCE_BROKER_BUSY      137
#define TCE_OFF_QUOTES       138
#define TCE_TOO_MANY_REQ     141
#define TCE_MODIFY_DENIED    145
#define TCE_CONTEXT_BUSY     146

string g_ovSym[];    // pip 覆盖表：品种名前缀
double g_ovPip[];    // pip 覆盖表：对应 pip 大小

//+------------------------------------------------------------------+
//| 初始化：解析 pip 覆盖表、启动定时器                                 |
//+------------------------------------------------------------------+
int OnInit()
{
   ParsePipOverrides();

   EventSetMillisecondTimer(MANAGE_TIMER_MS);

   if(InpInitialSLPips <= 0.0 && InpBETriggerPips <= 0.0 && !InpUseTrailing)
      Print(EA_NAME, " 警告：初始止损、保本触发、移动止损均未启用，EA 不会执行任何操作！");

   if(InpBETriggerPips > 0.0 && InpBEBufferPips >= InpBETriggerPips)
      Print(EA_NAME, " 提示：保本缓冲(", InpBEBufferPips, ") >= 触发点数(",
            InpBETriggerPips, ")，触发初期可能暂时无法落到目标价位，将自动取最近合法价位。");

   double point = MarketInfo(Symbol(), MODE_POINT);
   Print(EA_NAME, " 启动 | 范围=", (InpCurrentSymbolOnly ? Symbol() : "全账户"),
         " | 初始SL=", DoubleToString(InpInitialSLPips, 1), "pips",
         " | 保本触发=", DoubleToString(InpBETriggerPips, 1), "pips",
         " 缓冲=", DoubleToString(InpBEBufferPips, 1), "pips",
         " | 移动止损=", (InpUseTrailing ? "开" : "关"));
   Print(EA_NAME, " 图表品种 ", Symbol(),
         " point=", DoubleToString(point, 8),
         " 1pip=", DoubleToString(PipSize(Symbol()), 8),
         " (特殊品种点值请用 InpPipOverrides 校正)");

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| 反初始化：销毁定时器                                                |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| 主入口：OnTick 管当前品种即时性，OnTimer 兜底其它品种               |
//+------------------------------------------------------------------+
void OnTick()
{
   ManageAllOrders();
}

void OnTimer()
{
   ManageAllOrders();
}

//+------------------------------------------------------------------+
//| 遍历账户全部持仓，逐单管理                                          |
//+------------------------------------------------------------------+
void ManageAllOrders()
{
   if(!IsTradeAllowed())     // 自动交易按钮未开启或交易流不可用
      return;
   if(IsTradeContextBusy())  // 其他 EA 正在占用交易上下文，下一轮再处理
      return;

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
         continue;
      ManageOrder();
   }
}

//+------------------------------------------------------------------+
//| 管理单个持仓（要求该订单已被 OrderSelect 选中）                      |
//+------------------------------------------------------------------+
void ManageOrder()
{
   int type = OrderType();
   if(type != OP_BUY && type != OP_SELL)   // V1 只管市价持仓，不处理挂单
      return;

   string sym = OrderSymbol();
   if(InpCurrentSymbolOnly && sym != Symbol())
      return;

   double pip    = PipSize(sym);
   double point  = MarketInfo(sym, MODE_POINT);
   double openP  = OrderOpenPrice();

   // 防护：品种数据不可用（如未加入市场报价窗口）时 point/pip 为 0，除零会终止 EA
   if(pip <= 0.0 || point <= 0.0)
   {
      if(InpVerboseLog) Print(EA_NAME, " 品种 ", sym, " 点值数据为 0（未加入市场报价窗口？），跳过");
      return;
   }

   //--- 1. 初始止损：无止损则补挂固定点数止损 --------------------------
   if(OrderStopLoss() <= 0.0 && InpInitialSLPips > 0.0)
   {
      double raw = (type == OP_BUY)
                   ? openP - InpInitialSLPips * pip
                   : openP + InpInitialSLPips * pip;
      TryMoveSL(type, sym, raw, point, "INIT");
   }

   //--- 浮盈计算（以平仓侧价格为准：买用 Bid，卖用 Ask）-----------------
   double closeP     = (type == OP_BUY) ? MarketInfo(sym, MODE_BID)
                                        : MarketInfo(sym, MODE_ASK);
   double profitPips = (type == OP_BUY) ? (closeP - openP) / pip
                                        : (openP - closeP) / pip;

   //--- 2. 保本推损：达到触发点数后，把止损推到开仓价之上 ----------------
   if(InpBETriggerPips > 0.0 && profitPips >= InpBETriggerPips)
   {
      double raw = (type == OP_BUY)
                   ? openP + InpBEBufferPips * pip
                   : openP - InpBEBufferPips * pip;
      TryMoveSL(type, sym, raw, point, "BE");
   }

   //--- 3. 移动止损（默认关闭，V2 扩展点）-------------------------------
   if(InpUseTrailing && profitPips >= InpTrailStartPips)
   {
      double raw = (type == OP_BUY)
                   ? closeP - InpTrailDistPips * pip
                   : closeP + InpTrailDistPips * pip;
      TryMoveSL(type, sym, raw, point, "TRAIL", InpTrailStepPips * pip);
   }
}

//+------------------------------------------------------------------+
//| 尝试移动止损：clamp 到经纪商允许区间，仅在"向有利方向改善"时发单      |
//| minImprove：最小改善幅度（价格单位），用于移动止损的步进防抖          |
//+------------------------------------------------------------------+
void TryMoveSL(const int type, const string sym, const double rawSL,
               const double point, const string reason,
               const double minImprove = 0.0)
{
   double bid      = MarketInfo(sym, MODE_BID);
   double ask      = MarketInfo(sym, MODE_ASK);
   int    digits   = (int)MarketInfo(sym, MODE_DIGITS);
   double minDist  = MarketInfo(sym, MODE_STOPLEVEL)  * point;  // 最小止损距离
   double freeze   = MarketInfo(sym, MODE_FREEZELEVEL) * point; // 冻结距离

   //--- FREEZELEVEL：现有止损离市价过近时，经纪商会拒绝修改，本轮跳过 ----
   if(freeze > 0.0 && OrderStopLoss() > 0.0)
   {
      if(type == OP_BUY && (bid - OrderStopLoss()) < freeze)
      {
         if(InpVerboseLog) Print(EA_NAME, " #", OrderTicket(), " 现有止损进入冻结区，跳过本轮");
         return;
      }
      if(type == OP_SELL && (OrderStopLoss() - ask) < freeze)
      {
         if(InpVerboseLog) Print(EA_NAME, " #", OrderTicket(), " 现有止损进入冻结区，跳过本轮");
         return;
      }
   }

   double sl;
   double eps = MathMax(point * 0.5, minImprove);  // 改善阈值（含步进防抖）

   if(type == OP_BUY)
   {
      // 买入单止损必须低于 Bid 至少 minDist，超出则 clamp 到最近合法价位
      sl = MathMin(rawSL, bid - minDist);
      sl = NormalizeDouble(sl, digits);
      if(sl <= 0.0)
         return;
      if(OrderStopLoss() > 0.0 && (sl - OrderStopLoss()) < eps)
         return;   // 非有效改善（绝不放宽已有多头止损）
   }
   else
   {
      // 卖出单止损必须高于 Ask 至少 minDist
      sl = MathMax(rawSL, ask + minDist);
      sl = NormalizeDouble(sl, digits);
      if(OrderStopLoss() > 0.0 && (OrderStopLoss() - sl) < eps)
         return;   // 非有效改善（绝不放宽已有空头止损）
   }

   ModifySL(OrderTicket(), sl, reason, sym);
}

//+------------------------------------------------------------------+
//| 发送 OrderModify，带重试与错误分类                                  |
//+------------------------------------------------------------------+
bool ModifySL(const int ticket, const double newSL, const string reason,
              const string sym)
{
   for(int attempt = 1; attempt <= MODIFY_MAX_TRIES; attempt++)
   {
      if(!OrderSelect(ticket, SELECT_BY_TICKET))
      {
         Print(EA_NAME, " #", ticket, " 订单选择失败，放弃修改");
         return(false);
      }
      if(OrderCloseTime() > 0)   // 订单已在流程中平仓
         return(false);

      RefreshRates();
      int    digits = (int)MarketInfo(sym, MODE_DIGITS);
      double point  = MarketInfo(sym, MODE_POINT);
      double sl     = NormalizeDouble(newSL, digits);

      // 目标与现值几乎相同，视为已达成
      if(MathAbs(sl - OrderStopLoss()) < point * 0.5)
         return(true);

      ResetLastError();
      bool ok = OrderModify(ticket, OrderOpenPrice(), sl, OrderTakeProfit(), 0, clrDodgerBlue);
      if(ok)
      {
         Print(EA_NAME, " #", ticket, " ", sym,
               (OrderType() == OP_BUY ? " BUY" : " SELL"),
               " 止损 -> ", DoubleToString(sl, digits),
               " (", reason, ")");
         return(true);
      }

      int err = GetLastError();
      if(err == TCE_NO_RESULT)
         return(true);   // 服务器认为无实际变化，等同成功

      // 可重试的瞬时错误：重报价/价格变化/繁忙/断线等
      if(err == TCE_REQUOTE || err == TCE_PRICE_CHANGED || err == TCE_BROKER_BUSY ||
         err == TCE_OFF_QUOTES || err == TCE_SERVER_BUSY || err == TCE_CONTEXT_BUSY ||
         err == TCE_NO_CONNECTION || err == TCE_TOO_MANY_REQ)
      {
         if(InpVerboseLog)
            Print(EA_NAME, " #", ticket, " 修改重试 ", attempt, "/", MODIFY_MAX_TRIES,
                  "：", TradeErrText(err));
         Sleep(200);
         continue;
      }

      // 硬错误（如 INVALID_STOPS）：放弃本轮，下一轮由 TryMoveSL 重新 clamp
      Print(EA_NAME, " #", ticket, " 修改失败：", TradeErrText(err),
            " 目标SL=", DoubleToString(sl, digits));
      return(false);
   }
   return(false);
}

//+------------------------------------------------------------------+
//| pip 引擎：覆盖表优先，其次按小数位自适应                             |
//|   3/5 位报价（含分数 pip）→ 1 pip = 10 * point                      |
//|   其余（4 位直盘、2 位日元、黄金等）→ 1 pip = point                 |
//|   黄金等特殊品种请用 InpPipOverrides 校正，如 "XAUUSD=0.1"          |
//|   覆盖表按品种名前缀匹配，可兼容经纪商后缀（如 XAUUSD.m）             |
//+------------------------------------------------------------------+
double PipSize(const string symbol)
{
   for(int i = 0; i < ArraySize(g_ovSym); i++)
   {
      if(StringFind(symbol, g_ovSym[i]) == 0)
         return(g_ovPip[i]);
   }

   double point  = MarketInfo(symbol, MODE_POINT);
   int    digits = (int)MarketInfo(symbol, MODE_DIGITS);

   if(digits == 3 || digits == 5)
      return(point * 10.0);
   return(point);
}

//+------------------------------------------------------------------+
//| 解析 pip 覆盖表："XAUUSD=0.1;XAGUSD=0.01"                          |
//+------------------------------------------------------------------+
void ParsePipOverrides()
{
   ArrayResize(g_ovSym, 0);
   ArrayResize(g_ovPip, 0);
   if(StringLen(InpPipOverrides) == 0)
      return;

   string pairs[];
   int n = StringSplit(InpPipOverrides, ';', pairs);
   for(int i = 0; i < n; i++)
   {
      if(StringLen(pairs[i]) == 0)   // 兼容末尾多余的分号
         continue;
      string kv[];
      if(StringSplit(pairs[i], '=', kv) != 2)
      {
         Print(EA_NAME, " pip 覆盖项格式错误（应为 品种=点值）: ", pairs[i]);
         continue;
      }
      double v = StringToDouble(kv[1]);
      if(v <= 0.0)
      {
         Print(EA_NAME, " pip 覆盖项点值非法: ", pairs[i]);
         continue;
      }
      int idx = ArraySize(g_ovSym);
      ArrayResize(g_ovSym, idx + 1);
      ArrayResize(g_ovPip, idx + 1);
      g_ovSym[idx] = kv[0];
      g_ovPip[idx] = v;
   }
}

//+------------------------------------------------------------------+
//| 常见交易错误码转文本                                                 |
//+------------------------------------------------------------------+
string TradeErrText(const int code)
{
   switch(code)
   {
      case TCE_NO_RESULT:      return("无实际修改(NO_RESULT)");
      case TCE_SERVER_BUSY:    return("服务器繁忙");
      case TCE_NO_CONNECTION:  return("连接断开");
      case TCE_TIMEOUT:        return("等待超时");
      case TCE_INVALID_PRICE:  return("无效价格");
      case TCE_INVALID_STOPS:  return("无效止损(INVALID_STOPS)");
      case TCE_TRADE_DISABLED: return("交易被禁止");
      case TCE_REQUOTE:        return("重新报价(REQUOTE)");
      case TCE_PRICE_CHANGED:  return("价格已变化");
      case TCE_BROKER_BUSY:    return("经纪商忙");
      case TCE_OFF_QUOTES:     return("报价已关闭(OFF_QUOTES)");
      case TCE_TOO_MANY_REQ:   return("请求过于频繁");
      case TCE_MODIFY_DENIED:  return("修改被拒绝(距市价过近)");
      case TCE_CONTEXT_BUSY:   return("交易上下文忙");
   }
   return("错误码 " + IntegerToString(code));
}
//+------------------------------------------------------------------+
