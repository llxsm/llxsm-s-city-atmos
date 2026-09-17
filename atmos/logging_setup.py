"""结构化日志。

统一日志格式便于在终端与后续日志采集中定位采集批次与资产。
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", *, force: bool = False) -> None:
    """配置根 logger（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    if force:
        root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 降低三方库噪声
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的 logger。"""
    return logging.getLogger(name if name.startswith("atmos") else f"atmos.{name}")
