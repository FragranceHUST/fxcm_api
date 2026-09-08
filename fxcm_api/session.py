"""会话管理：登录/登出 ForexConnect（use_table_manager=True 以获得自动刷新的表格）。

登录内置瞬时失败重试（退避 5s/15s）——FXCM 对同一账户的高频重登会节流，
连续 CLI 命令各自行 login/logout 时容易触发 30s 等待超时。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from forexconnect import ForexConnect, fxcorepy

from fxcm_api.config import Credentials

logger = logging.getLogger("fxcm_api.session")

StatusCallback = Callable[[Any, Any], None]

RETRY_DELAYS = (5.0, 15.0)


def _is_transient(exc: Exception) -> bool:
    text = str(exc)
    return "Wait timeout exceeded" in text or "Not connected" in text


def _login_once(fx: ForexConnect, cred: Credentials,
                on_status: StatusCallback | None) -> None:
    login_kwargs: dict[str, Any] = {
        "user_id": cred.user_id,
        "password": cred.password,
        "url": cred.url,
        "connection": cred.connection,
        "use_table_manager": True,
    }
    if cred.session_id:
        login_kwargs["session_id"] = cred.session_id
    if cred.pin:
        login_kwargs["pin"] = cred.pin
    if on_status is not None:
        login_kwargs["session_status_callback"] = on_status

    fx.login(**login_kwargs)


def connect(cred: Credentials, on_status: StatusCallback | None = None,
            retries: int = 2) -> ForexConnect:
    if not cred.is_complete():
        raise RuntimeError("缺少 FXCM 凭据：请在配置文件或 FXCM_USER_ID/FXCM_PASSWORD 环境变量中提供")

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        fx = ForexConnect()
        try:
            _login_once(fx, cred, on_status)
            logger.info("已连接 %s (%s)", cred.url, cred.connection)
            return fx
        except Exception as exc:
            last_exc = exc
            disconnect(fx)   # 清理半建立的会话（包装器超时路径不会自己登出）
            if attempt >= retries or not _is_transient(exc):
                raise
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            logger.warning("登录失败（%s），%g 秒后重试 %d/%d",
                           exc, delay, attempt + 1, retries)
            time.sleep(delay)
    raise last_exc   # pragma: no cover - 循环内必然 return 或 raise


def disconnect(fx: ForexConnect | None) -> None:
    if fx is None:
        return
    try:
        # 包装器 logout() 结束时会把内部 _session 置 None，必须先抓引用
        session = fx.session
        fx.logout()
        # 官方建议：释放会话前等到 DISCONNECTED，避免影响下一次登录
        if session is not None:
            deadline = time.time() + 5.0
            disconnected = fxcorepy.AO2GSessionStatus.O2GSessionStatus.DISCONNECTED
            while time.time() < deadline:
                if session.session_status == disconnected:
                    break
                time.sleep(0.2)
    except Exception as exc:      # 登出失败不应中断退出流程
        logger.warning("logout 异常: %s", exc)
