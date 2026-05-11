"""共用 logger 配置：统一输出到 stdout + 文件。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)


def setup_file_logger(name: str, log_file: Path, level: int = logging.INFO) -> logging.Logger:
    """返回名为 `name` 的 logger，幂等地挂上 stdout + log_file 两个 handler。"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:
        return logger

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(_FORMAT)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(_FORMAT)
    logger.addHandler(sh)
    return logger
