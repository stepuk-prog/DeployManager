"""Суб-инструмент «📤 Git push» — обзор репозиториев PROJECTS_DIR и push выбранных.

Логика — в `core/gitpush.py` (её же зовёт гейт деплоя `ensure_pushed`); здесь только точка
входа реестра tools/ (kind=flow: `async run(db)`).
"""
from core.gitpush import run

__all__ = ["run"]
