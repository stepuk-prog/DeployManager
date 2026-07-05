"""Резолв селекторов pocket из Program.settings.pocket_settings.

В отличие от старого lazy-`_PocketConfig` (sync `__getattr__` → sync DB), здесь БД
асинхронная: строки настроек читаем один раз в начале флоу (`await db.pocket_settings()`)
и превращаем в готовый dict логический-ключ → значение селектора.
"""
from tools.cookies.settings.constant import POCKET_PARAM_NAMES


def find_par(rows, par_name: str, default=None):
    """Значение par_value по par_name в строках settings.pocket_settings."""
    for row in rows:
        if row["par_name"] == par_name:
            return row["par_value"]
    return default


def resolve_selectors(rows) -> dict:
    """rows (settings.pocket_settings) → {логический_ключ: значение_селектора}."""
    return {key: find_par(rows, par_name) for key, par_name in POCKET_PARAM_NAMES.items()}
