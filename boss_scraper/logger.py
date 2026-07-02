"""
日志系统模块

提供统一的日志配置，支持控制台输出和文件轮转记录。
所有模块通过 get_logger() 获取各自的 logger 实例。
"""

import os
import logging
from logging.handlers import RotatingFileHandler

# 日志目录
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(name)s] [%(levelname)s] %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _init_root_logger():
    """初始化根 logger，添加控制台和文件 handler"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger("boss_scraper")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    if root.handlers:
        return

    # 控制台 handler (INFO 级别)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATE_FMT))
    root.addHandler(console)

    # 文件 handler (DEBUG 级别, 轮转: 5MB x 3 份)
    log_file = os.path.join(_LOG_DIR, "scraper.log")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATE_FMT))
    root.addHandler(file_handler)


def get_logger(name: str = "boss_scraper") -> logging.Logger:
    """获取 logger 实例

    Args:
        name: 模块名称, 通常传 __name__

    Returns:
        logging.Logger 实例
    """
    _init_root_logger()
    if not name.startswith("boss_scraper"):
        name = f"boss_scraper.{name}"
    return logging.getLogger(name)
