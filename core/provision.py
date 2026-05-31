"""Детекция пакетов из requirements.txt, требующих отдельной установки после pip.

Сейчас известен playwright (нужен `playwright install <browser>` — бинарь браузера).
Карта расширяемая: добавить пакет → команда (без префикса venv/bin/, его добавит provision).
"""
import os
import re

from settings import config

# имя пакета (lower) → команда отдельной установки (внутри venv, без 'venv/bin/')
_POST_INSTALL = {
    "playwright": lambda: f"playwright install {config.PLAYWRIGHT_BROWSER}",
}


def _requirement_names(project_dir: str) -> set[str]:
    """Имена пакетов из requirements.txt (без версий/extras)."""
    path = os.path.join(project_dir, "requirements.txt")
    names: set[str] = set()
    if not os.path.exists(path):
        return names
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            names.add(m.group(1).lower())
    return names


def detect_post_install(project_dir: str) -> list[tuple[str, str]]:
    """[(пакет, команда)] для пакетов, требующих отдельной установки."""
    names = _requirement_names(project_dir)
    return [(pkg, make_cmd()) for pkg, make_cmd in _POST_INSTALL.items() if pkg in names]