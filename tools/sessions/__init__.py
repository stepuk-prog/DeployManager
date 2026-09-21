"""Суб-инструмент «Юзерботы (сессии)»: логин юзерботов (pyrofork/Telethon) → session_string.

ДВЕ ГРУППЫ юзерботов, и это разделение по базам, а не по вкусу:

  🛠 Флот      — `Program.telegram.telegram`: аккаунты, которыми работают программы флота.
                 Привязка к программе (`programdata.user_bot`), Desktop — из справочника
                 `telegram.telegram_apps`. Ветки в `fleet.py`.
  🎭 Персонажи — `forum.person.account`: аккаунты ИИ-персонажей форума (ForumPersonas).
                 Привязка к персонажу (`person.persona`), Desktop — одна программа с
                 `-workdir` слота, две колонки сессий (pyrofork + Telethon), свой набор
                 проверок (живость, приватность). Ветки в `personas.py`.

Мешать их в одном списке нельзя: базы разные, сджойнить нечем, а запись сессии не в ту
таблицу молча ломает и аккаунт, и программу, которая им работает.

Точка входа `run(db)` — выбор группы, дальше под-меню группы. Пул БД Program поднимает
caller (cli.run); пул БД forum поднимает и закрывает сама группа персонажей.
"""
from core import ui
from database import Database
from tools.sessions import fleet, personas

_GROUPS = [
    ("fleet", "🛠 Юзерботы флота (БД Program)"),
    ("person", "🎭 Персонажи форума (БД forum)"),
]

# Действия старого, «одногруппного» меню — чтобы `--action sessions --command list`
# и вызовы из GUI, написанные до разделения, продолжали попадать во флот.
_FLEET_ACTIONS = {"list", "recover", "create"}


async def run(db: Database, action: str | None = None) -> None:
    """Выбор группы юзерботов, затем её под-меню.

    action задан и это старое флотовое действие → идём во флот без вопроса (обратная
    совместимость). action вида `person:<ветка>` → сразу в нужную ветку персонажей."""
    if action in _FLEET_ACTIONS:
        await fleet.run(db, action)
        return
    if action and action.startswith("person"):
        await personas.run(action.split(":", 1)[1] if ":" in action else None)
        return

    idx = await ui.select("С какой группой юзерботов работаем?", [label for _, label in _GROUPS])
    if idx is None:
        print("✖️ Отменено.")
        return
    if _GROUPS[idx][0] == "fleet":
        await fleet.run(db)
    else:
        await personas.run()
