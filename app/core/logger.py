"""日志配置"""

import sys
import logging
from pathlib import Path


def setup_logger():
    """根据 DEBUG 配置设置日志"""
    # 延迟导入避免循环依赖
    from app.core.config import settings, ROOT_DIR

    handlers = [logging.StreamHandler(sys.stdout)]

    # DEBUG 模式下保存日志到本地文件
    if settings.DEBUG:
        log_file = ROOT_DIR / "log.txt"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        handlers.append(file_handler)

    # 配置日志格式
    logging.basicConfig(
        level=logging.DEBUG if settings.DEBUG else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True  # 强制重新配置
    )

    _logger = logging.getLogger("grok-imagine")
    _logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    return _logger


logger = setup_logger()
