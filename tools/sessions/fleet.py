"""Ветки группы «Юзерботы флота»: аккаунты из БД Program (`telegram.telegram`).

Первая, историческая группа: юзерботы, которыми работают программы флота (BinoOptions,
TradingSignals и прочие). Привязка — к ПРОГРАММЕ (`program.programdata.user_bot`),
Desktop-клиент берётся из справочника `telegram.telegram_apps` по колонке `my_gram`.
Вынесено из `__init__.py` при разделении на две группы: рядом появилась группа
персонажей, которая живёт в другой базе (см. `personas.py`).
"""
from core import ui
from database import Database
from tools.sessions import recover

_ACTIONS = [
    ("list", "📋 Список юзерботов"),
    ("recover", "♻️ Обновить сессию (есть session_string)"),
    ("create", "➕ Создать сессию (без session_string)"),
]


async def _list(db: Database) -> None:
    """Read-only таблица юзерботов со статусами (без секретов)."""
    rows = await db.list_userbots()
    if not rows:
        print("В telegram.telegram нет юзерботов.")
        return
    print(f"\nЮзерботов: {len(rows)}\n")
    print(f"{'id_telegram':>12}  {'имя':<24} {'телефон':<16} "
          f"{'api':<4} {'sess':<5} {'mail':<5} {'почта'}")
    print("-" * 100)
    for r in rows:
        api = "✅" if r["api_id"] and r["has_hash"] else "—"
        sess = "✅" if r["has_session"] else "—"
        mailp = "✅" if r["has_mailpass"] else "—"
        print(f"{r['id_telegram']:>12}  {(r['name'] or '')[:24]:<24} "
              f"{(r['phone'] or '—'):<16} {api:<4} {sess:<5} {mailp:<5} {r['mail'] or '—'}")
    have = sum(1 for r in rows if r["has_session"])
    print(f"\nС сессией: {have} / {len(rows)}; без сессии: {len(rows) - have}.")


async def run(db: Database, action: str | None = None) -> None:
    """Под-меню группы «Флот». action ∈ {list, recover, create} или None (спросить).
    Пул БД Program уже поднят caller'ом (cli.run) — здесь только ветки поверх `db`."""
    if not action:
        idx = await ui.select("Юзерботы флота — что делаем?", [label for _, label in _ACTIONS])
        if idx is None:
            print("✖️ Отменено.")
            return
        action = _ACTIONS[idx][0]

    if action == "list":
        await _list(db)
    elif action == "recover":
        await recover.recover_session(db)
    elif action == "create":
        await recover.recover_session(db, only_without_session=True)
    else:
        print(f"Неизвестное действие: {action}")
