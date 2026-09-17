"""双会话管理：real（数据面）+ demo（账户面），各自监督线程，错峰登录，断线自愈。

监督规则：CONNECTED 正常；RECONNECTING = 原生自动重连中（等待）；其余状态按退避序列重连。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from forexconnect import ForexConnect

from fxcm_api.config import load_config, load_daemon_settings
from fxcm_api.session import connect, disconnect

logger = logging.getLogger("fxcm_api.sessions")

_RECONNECT_BACKOFF = (15, 30, 60, 120)
_STALE_RELOAD_MIN_INTERVAL = 600.0   # 假死强制重登录最小间隔（防 FXCM 登录节流风暴）


def fx_market_open(now: float | None = None) -> bool:
    """FX 交易时段判定（美东时间）：周五 17:00 收盘 → 周日 17:00 开盘；
    每日 16:55–17:10 滚动维护窗口视为休市。"""
    dt = datetime.fromtimestamp(now if now is not None else time.time(),
                                ZoneInfo("America/New_York"))
    t = dt.time()
    if dt.weekday() == 5:                          # 周六
        return False
    if dt.weekday() == 4 and t >= dt_time(17, 0):  # 周五收盘后
        return False
    if dt.weekday() == 6 and t < dt_time(17, 0):   # 周日开盘前
        return False
    if dt_time(16, 55) <= t < dt_time(17, 10):     # 每日维护窗口
        return False
    return True


class SessionWorker:
    """单环境会话持有者。start() 后台线程先连接一次，随后进入监督循环。"""

    def __init__(self, env: str, config_path: str):
        self.env = env
        self.config_path = config_path
        self.cred, self.guard = load_config(config_path)
        self.daemon_cfg = load_daemon_settings(config_path)
        self.fx = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"session-{env}", daemon=True)
        self._connected_once = threading.Event()
        self._backoff_idx = 0
        self._last_error = ""
        self.on_reconnect: Callable[[object], None] | None = None   # 重连成功后回调（daemon 换绑 hub 等订阅）
        self.staleness_check: Callable[[], bool] | None = None      # CONNECTED 但数据面停更时返回 True（仅 real 数据面）
        self._last_stale_reload = 0.0

    def start(self, wait_connected: float = 0.0) -> bool:
        """启动会话线程；wait_connected>0 时等待首次连接结果。"""
        self._thread.start()
        if wait_connected > 0:
            return self._connected_once.wait(wait_connected) and self.fx is not None
        return True

    def _run(self) -> None:
        self._connect()
        self._connected_once.set()
        while not self._stop.is_set():
            time.sleep(5)
            if self._stop.is_set():
                break
            status = self._status()
            if status == "CONNECTED":
                self._backoff_idx = 0
                if self._stale_reload_due():
                    self._last_stale_reload = time.time()
                    logger.warning("[%s] 行情停更（会话假死），强制重登录", self.env)
                    disconnect(self.fx)
                    self.fx = None
                    self._connect()
                continue
            if status == "RECONNECTING":
                continue                       # 原生自动重连中（默认最多 20 次）
            delay = _RECONNECT_BACKOFF[min(self._backoff_idx, len(_RECONNECT_BACKOFF) - 1)]
            self._backoff_idx += 1
            logger.warning("[%s] 会话状态 %s，%gs 后重连", self.env, status, delay)
            if self._stop.wait(delay):
                break
            disconnect(self.fx)
            self.fx = None
            self._connect()
        disconnect(self.fx)

    def _connect(self) -> None:
        try:
            self.fx = connect(self.cred, retries=4)
            logger.info("[%s] 会话就绪", self.env)
            if self._connected_once.is_set() and self.on_reconnect:
                try:
                    self.on_reconnect(self.fx)
                except Exception:
                    logger.exception("[%s] on_reconnect 回调失败", self.env)
        except Exception as exc:
            self._last_error = str(exc)[:120]
            self.fx = None
            logger.error("[%s] 连接失败: %s", self.env, self._last_error)

    def _status(self) -> str:
        if self.fx is None:
            return "NONE"
        try:
            return self.fx.session.session_status.name if self.fx.session else "NONE"
        except Exception:
            return "NONE"

    def _stale_reload_due(self) -> bool:
        if self.staleness_check is None:
            return False
        if time.time() - self._last_stale_reload < _STALE_RELOAD_MIN_INTERVAL:
            return False
        try:
            return bool(self.staleness_check())
        except Exception:
            logger.exception("[%s] staleness_check 异常，跳过本周期", self.env)
            return False

    def is_ready(self) -> bool:
        """可交易判定：仅 CONNECTED 状态下原生 request_factory 非空（下单依赖它）。"""
        return self._status() == "CONNECTED"

    def status(self) -> dict:
        return {"env": self.env, "status": self._status(),
                "guard_enabled": self.daemon_cfg.guard_enabled,
                "error": self._last_error}

    def account_summary(self) -> dict | None:
        if self.fx is None:
            return None
        try:
            accounts = self.fx.get_table(ForexConnect.ACCOUNTS)
            if accounts is None or accounts.size == 0:
                return None
            row = accounts.get_row(0)
            return {"account": str(row.account_id),
                    "balance": float(row.balance),
                    "equity": float(getattr(row, "equity", row.balance) or row.balance),
                    "margin_used": float(getattr(row, "margin_used", 0.0) or 0.0)}
        except Exception as exc:
            logger.warning("[%s] 账户摘要读取失败: %s", self.env, exc)
            return None

    def stop(self) -> None:
        self._stop.set()


class SessionManager:
    """real + demo 双会话。real 先连（数据面依赖），错峰启动防节流。"""

    def __init__(self, real_config: str, demo_config: str, stagger_s: float = 10.0):
        self.real = SessionWorker("real", real_config)
        self.demo = SessionWorker("demo", demo_config)
        self._stagger_s = stagger_s

    def start(self, wait_connected: float = 45.0) -> tuple[bool, bool]:
        ok_real = self.real.start(wait_connected=wait_connected)
        if not ok_real:
            return False, False             # 数据面不可用则不启动 demo，避免无效登录
        time.sleep(self._stagger_s)
        ok_demo = self.demo.start(wait_connected=wait_connected)
        return ok_real, ok_demo

    def worker(self, env: str) -> SessionWorker:
        return self.real if env == "real" else self.demo

    def status(self) -> dict:
        return {"real": self.real.status(), "demo": self.demo.status()}

    def stop(self) -> None:
        self.real.stop()
        self.demo.stop()
