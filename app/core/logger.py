"""日志配置"""

import sys
import logging
from pathlib import Path


def setup_logger():
    """根据 DEBUG 配置设置日志"""
    # 延迟导入避免循环依赖
    from app.core.config import settings, ROOT_DIR

    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]

    # DEBUG 模式下保存日志到本地文件
    if settings.DEBUG:
        log_file = ROOT_DIR / "log.txt"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
        handlers.append(file_handler)

    # 配置根日志
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
        force=True
    )

    # 配置 uvicorn 日志使用相同格式
    for uvicorn_logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers = handlers.copy()
        uvicorn_logger.setLevel(log_level)

    # 项目 logger
    _logger = logging.getLogger("grok-imagine")
    _logger.setLevel(log_level)
    return _logger


# Uvicorn 日志配置（传递给 uvicorn.run）
def get_uvicorn_log_config():
    """获取 uvicorn 日志配置"""
    from app.core.config import settings

    log_level = "DEBUG" if settings.DEBUG else "INFO"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": log_level, "propagate": False},
        },
    }


logger = setup_logger()
