"""Запуск Telegram Desktop клиента (привязка my_gram → telegram.telegram_apps).

При логине код подтверждения приходит в Telegram-аккаунт; чтобы оператор взял его из
работающего клиента, по «Старт» поднимаем нужный Desktop-клиент (exec_path/workdir из
справочника telegram_apps) и показываем окошко «загружен» с кнопками «Дальше»/«Отмена».
Best-effort: сбой запуска НЕ срывает логин — оператор может продолжить вручную.
"""
import subprocess

from core import ui
from database.db import Database
from logs import get_logger

logger = get_logger(__name__)


def _spawn(exec_path: str, workdir: str) -> bool:
    """Поднять Desktop-клиент отдельным detached-процессом (не блокируя event-loop).

    Telegram Desktop берёт каталог данных (tdata = нужный аккаунт) из аргумента
    `-workdir`, а НЕ из cwd. Без него стартует системный профиль — поэтому передаём
    workdir явным флагом (как в системных .desktop-ярлыках: `Telegram -workdir <wd>`)."""
    cmd = [exec_path]
    if workdir:
        cmd += ["-workdir", workdir]
    try:
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except (Exception,) as error:
        logger.warning("Не удалось запустить Telegram-клиент %r: %s", exec_path, error)
        print(f"⚠️ Не удалось запустить Telegram-клиент: {error}")
        return False


async def launch_for(db: Database, app_name: str | None) -> bool:
    """Запустить клиент по app_name (my_gram) и спросить подтверждение продолжения.

    Возвращает True — продолжать операцию (логин), False — оператор нажал «Отмена».
    app_name пуст/не найден в справочнике → запуск пропускаем, но операцию НЕ прерываем
    (логин возможен и без поднятого клиента: код может прийти по SMS / в другой сессии)."""
    if not app_name:
        print("ℹ️ Telegram-клиент не задан (my_gram пуст) — запуск пропущен.")
        return True
    app = await db.get_app(app_name)
    if app is None:
        print(f"⚠️ Telegram-клиент «{app_name}» не найден в справочнике — запуск пропущен.")
        return True
    print(f"🚀 Запускаю Telegram-клиент «{app_name}»…")
    ok = _spawn(app["exec_path"], app["workdir"])
    msg = (f"🚀 Telegram-клиент «{app_name}» загружен.\n"
           f"Возьмите из него код подтверждения и нажмите «Дальше»."
           if ok else
           f"⚠️ Не удалось запустить клиент «{app_name}». Продолжить логин вручную?")
    return await ui.confirm(msg, ok_label="➡️ Дальше", cancel_label="✖️ Отмена")
