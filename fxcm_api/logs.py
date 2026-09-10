"""日志装配：统一 basicConfig 并过滤 forexconnect 包装器的已知误报。"""

from __future__ import annotations

import logging


class SendRequestThreadNoiseFilter(logging.Filter):
    """丢弃包装器的"非主线程 send_request"告警。

    冻结风险仅存在于 fxcorepy 事件回调内同步调用（阻塞响应分发线程→自锁）；
    本项目全部 9 处调用（guard/web/backfill）均为普通工作线程，无回调内调用，
    包装器无法区分两种场景，只能全量告警——依据 AGENTS.md 结论定向过滤。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "not from the main thread" not in record.getMessage()


def configure(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(SendRequestThreadNoiseFilter())
