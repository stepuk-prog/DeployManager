"""Журнал ввода узла (resume) для «Настроить ноду».

Первый запуск пишет введённые данные + прогресс по шагам в JSON (по одному файлу на IP),
повторный — грузит журнал, ПОВТОРНО не опрашивает и продолжает с первого не-`done` шага.
Пароль root В ЖУРНАЛ НЕ ПИШЕТСЯ (секрет; на resume он обычно и не нужен — доступ по ключу).

Статусы шага: "done" | "failed" | "skipped" (или отсутствует = ещё не трогали).
"""
import ipaddress
import json
import os
from datetime import datetime

# logs/setup_node/<ip>.json — рядом с deploy_audit.log
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "setup_node")

DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


def _path(ip: str) -> str:
    ipaddress.ip_address(ip)                 # защита от подстановки пути через «ip»
    return os.path.join(_DIR, f"{ip}.json")


def load(ip: str) -> dict | None:
    """Журнал по IP или None (первый запуск)."""
    try:
        with open(_path(ip), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def create(ip: str, hostname: str, server_name: str, node_type: str) -> dict:
    """Создать журнал первого запуска и СРАЗУ записать (данные больше не переспрашиваем)."""
    now = datetime.now().isoformat(timespec="seconds")
    j = {
        "ip": ip, "hostname": hostname, "server_name": server_name,
        "node_type": node_type, "node_id": None,
        "created": now, "updated": now, "steps": {},
    }
    save(j)
    return j


def save(j: dict) -> None:
    os.makedirs(_DIR, exist_ok=True)
    j["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = _path(j["ip"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path(j["ip"]))          # атомарная замена — журнал не бьётся при обрыве


def step_status(j: dict, step: str) -> str | None:
    return j.get("steps", {}).get(step)


def set_step(j: dict, step: str, status: str) -> None:
    j.setdefault("steps", {})[step] = status
    save(j)


def set_field(j: dict, key: str, value) -> None:
    j[key] = value
    save(j)


def is_done(j: dict, step: str) -> bool:
    return step_status(j, step) == DONE


__all__ = ["load", "create", "save", "step_status", "set_step", "set_field",
           "is_done", "DONE", "FAILED", "SKIPPED"]
