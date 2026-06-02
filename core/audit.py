"""Audit-лог деплоев: одна JSON-строка на запуск (кто/когда/SHA/ноды/результат)."""
import json
import os

from logs import get_logger
from settings import config

logger = get_logger(__name__)


def write(record: dict) -> None:
    """Дописать запись в AUDIT_LOG (jsonl). Ошибку только логируем — деплой не валим."""
    try:
        if d := os.path.dirname(config.AUDIT_LOG):       # пустой dirname → makedirs("") падает
            os.makedirs(d, exist_ok=True)
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as f:
            # default=str — не терять запись на datetime/Record/Path и пр. несериализуемом
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        logger.info("audit → %s", config.AUDIT_LOG)
    except Exception as e:
        logger.warning("Не удалось записать audit-лог: %s", e)
