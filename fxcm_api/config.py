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
    be_trigger_pips: float = 5.0      # 浮盈达到该点数后推保本，0 = 关闭
    be_buffer_pips: float = 1.0       # 止损推至开仓价±该点数
    use_trailing: bool = False        # 移动止损（V2 预留，默认关）
    trail_start_pips: float = 10.0
    trail_dist_pips: float = 8.0
    trail_step_pips: float = 1.0      # 止损至少改善该点数才发修改请求（防抖）
    min_stop_distance_pips: float = 0.5   # 本地安全距离：新止损距市价不得小于该值
    poll_interval_ms: int = 500       # 轮询周期，对标 MQL4 版定时器
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


def load_daemon_settings(path: str | None = None) -> DaemonSettings:
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        known = {f.name for f in fields(DaemonSettings)}
        kwargs = {k: v for k, v in raw.get("daemon", {}).items() if k in known}
        return DaemonSettings(**kwargs)
    return DaemonSettings()
