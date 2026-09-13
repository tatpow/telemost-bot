import logging
import sys

import config


def get_logger(name: str) -> logging.Logger:
    """Единый формат логов для всего проекта: время | уровень | модуль | сообщение."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # уже настроен (например, при повторном импорте)

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.DEBUG)
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.propagate = False
    return logger
