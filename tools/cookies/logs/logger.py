"""Консольный логгер. Пишет в ТЕКУЩИЙ sys.stdout — в GUI он перенаправлен в лог-панель
Flet (gui/log_sink.py), поэтому логи (warning/ошибки БД/браузера) попадают и туда, а не
только в терминал. init_logger идемпотентен, propagate=False."""
import logging
import sys

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _StdoutProxy:
    """Прокси к актуальному sys.stdout (он подменяется на лог-панель в GUI)."""

    @staticmethod
    def write(s):
        return sys.stdout.write(s)

    @staticmethod
    def flush():
        try:
            sys.stdout.flush()
        except (Exception,):
            pass


def init_logger(name: str = "cookies") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(_StdoutProxy())
    handler.setFormatter(logging.Formatter(_FMT, "%H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


get_logger = init_logger
