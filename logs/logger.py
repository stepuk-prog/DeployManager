"""Минимальный консольный логгер."""
import logging
import sys

_FMT = "%(asctime)s %(levelname)-7s %(message)s"


class _StdoutProxy:
    """Пишет в ТЕКУЩИЙ sys.stdout. В GUI он перенаправлен в лог-панель, поэтому
    логгер (INFO/ошибки SSH/БД) попадает и туда, а не только в терминал."""

    @staticmethod
    def write(s):
        return sys.stdout.write(s)

    @staticmethod
    def flush():
        try:
            sys.stdout.flush()
        except (Exception,):
            pass


def get_logger(name: str = "deploy") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(_StdoutProxy())
    handler.setFormatter(logging.Formatter(_FMT, "%H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
