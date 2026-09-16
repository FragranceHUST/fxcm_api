"""配置加载：config JSON + 环境变量覆盖（FXCM_USER_ID / FXCM_PASSWORD 优先）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class Credentials:
    user_id: str = ""
    password: str = ""
    url: str = "www.fxcorporate.com/Hosts.jsp"
    connection: str = "real"          # demo / real
    session_id: str = ""              # 多数据库用户才需要
    pin: str = ""                     # 有 PIN 的用户才需要

    def is_complete(self) -> bool:
        return bool(self.user_id and self.password)


@dataclass
class GuardSettings:
    initial_sl_pips: float = 20.0     # 0 = 不自动加初始止损
    initial_sl_atr_mult: float = 0.0  # >0：初始止损=倍数×ATR(H4,12)（随波动率），覆盖 initial_sl_pips
    be_trigger_pips: float = 5.0      # 浮盈达到该点数后推保本，0 = 关闭
    be_trigger_pips_by_side: dict[str, float] | None = None   # {"buy":10,"sell":8} 按方向覆盖 be_trigger_pips
    be_trigger_pips_by_custom_id: dict[str, float] | None = None  # {"quad-F1S":8} 按持仓 CUSTOM_ID 覆盖（策略臂优先于方向）
    be_buffer_pips: float = 1.0       # 止损推至开仓价±该点数
    use_trailing: bool = False        # 移动止损（V2 预留，默认关）
    trail_start_pips: float = 10.0
    trail_dist_pips: float = 8.0
    trail_step_pips: float = 1.0      # 止损至少改善该点数才发修改请求（防抖）
    min_stop_distance_pips: float = 0.5   # 本地安全距离：新止损距市价不得小于该值
    poll_interval_ms: int = 500       # 轮询周期
    dry_run: bool = False             # True = 只打印将执行的动作，绝不发修改请求（观察模式）
    symbol_filter: list[str] | None = None   # None = 全品种；如 ["EUR/USD", "USD/JPY"]
    pip_overrides: dict[str, float] = field(default_factory=lambda: {"XAU/USD": 0.1})
    verbose: bool = False
    account_id: str = ""              # 空 = 自动取账户表第一行


def load_config(path: str | None = None) -> tuple[Credentials, GuardSettings]:
    cred_kwargs: dict[str, Any] = {}
    guard_kwargs: dict[str, Any] = {}

    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        known_cred = {f.name for f in fields(Credentials)}
        known_guard = {f.name for f in fields(GuardSettings)}
        cred_kwargs = {k: v for k, v in raw.get("credentials", {}).items() if k in known_cred}
        guard_kwargs = {k: v for k, v in raw.get("guard", {}).items() if k in known_guard}

    if os.environ.get("FXCM_USER_ID"):
        cred_kwargs["user_id"] = os.environ["FXCM_USER_ID"]
    if os.environ.get("FXCM_PASSWORD"):
        cred_kwargs["password"] = os.environ["FXCM_PASSWORD"]

    return Credentials(**cred_kwargs), GuardSettings(**guard_kwargs)


@dataclass
class DaemonSettings:
    watch_symbols: list[str] = field(default_factory=lambda: ["XAU/USD", "USD/JPY", "EUR/USD"])
    port: int = 8911                  # Web 服务监听端口（绑定 127.0.0.1）
    data_dir: str = "data"            # 本地存储目录（candles.db）
    guard_enabled: bool = True        # daemon 内置 guard 循环开关
    allow_trading: bool = True        # 本环境是否允许下单（real 建议保持默认并依赖二次确认）
    startup_backfill_days: float = 7.0  # 启动补洞窗口（天），0=关闭；覆盖停机缺口，超长缺口用回填脚本
    spread_log_interval_sec: float = 5.0  # 点差记录器采样间隔（秒），0=关闭


def load_daemon_settings(path: str | None = None) -> DaemonSettings:
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        known = {f.name for f in fields(DaemonSettings)}
        kwargs = {k: v for k, v in raw.get("daemon", {}).items() if k in known}
        return DaemonSettings(**kwargs)
    return DaemonSettings()


@dataclass
class ArmConfig:
    """quad 策略单臂：固定方向 + 入场阈值 + 波动率出场 + 保本触发。"""
    name: str
    direction: str          # long / short
    param1: float           # 带宽 = ATR × param1（入场阈值）
    tp_atr_mult: float      # TP 距离 = ATR × 该倍数（入场锁定）
    be_pips: float          # 浮盈达该点数 → guard 推保本


@dataclass
class StrategySettings:
    """vol_reversal 四臂实盘 runner 配置（live 语义与 quant 引擎对齐）。"""
    enabled: bool = False
    symbol: str = "EUR/USD"
    env: str = "demo"                 # 在哪个环境交易
    quantity: int = 150000            # 每臂数量（150,000 = 1.5 标准手）
    sl_atr_mult: float = 1.5          # SL 距离 = ATR × 该倍数（入场锁定）
    atr_period: int = 12
    poll_interval_s: float = 2.0      # m1 收线检测轮询周期
    dry_run: bool = True              # True = 只记录信号不发单
    custom_id_prefix: str = "quad"    # 持仓归因前缀：{prefix}-{arm.name}
    arms: list[ArmConfig] = field(default_factory=list)

    def arm_custom_id(self, arm: ArmConfig) -> str:
        return f"{self.custom_id_prefix}-{arm.name}"


def load_strategy_settings(path: str | None = None) -> StrategySettings:
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        cfg = raw.get("strategy")
        if not isinstance(cfg, dict):
            return StrategySettings()
        known = {f.name for f in fields(StrategySettings)}
        kwargs = {k: v for k, v in cfg.items() if k in known}
        arms = [ArmConfig(**a) for a in cfg.get("arms", []) if isinstance(a, dict)]
        kwargs["arms"] = arms
        return StrategySettings(**kwargs)
    return StrategySettings()
