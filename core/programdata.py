"""Интерактивное создание записи program.programdata.

Поля: service_name/folder — авто из service-файла (можно переопределить),
description/program_name — ввод вручную, author — выбор из реально используемых
(Proger M1 / Толстый …). program_id = MAX+1. Остальное — дефолты (флаги false, FK NULL).
"""
from core import ui
from database import Database
from logs import get_logger

logger = get_logger(__name__)


def _ask(prompt: str, default: str | None = None) -> str:
    return ui.ask(prompt, default or "")


async def _pick_author(db: Database) -> int | None:
    authors = await db.get_known_authors()
    if not authors:
        return None
    print("  Автор:")
    for i, a in enumerate(authors, 1):
        print(f"    [{i}] {a['name'] or '?'} ({a['telegram_name'] or '—'})  id={a['author']}  "
              f"использован в {a['used']} прогр.")
    while True:
        sel = _ask("  Выбор автора (номер, пусто — без автора)", "")
        if sel == "":
            return None
        if sel.isdigit() and 1 <= int(sel) <= len(authors):
            return authors[int(sel) - 1]["author"]
        print("    Не понял, повтори.")


async def create_record_interactive(db: Database, service_name: str | None = None,
                                     folder: str | None = None) -> bool:
    """Создать запись programdata. service_name/folder — предзаполнены из файла (если есть)."""
    print("\n── Создание записи program.programdata ──")
    service_name = _ask("  service_name", service_name)
    if not service_name:
        print("  service_name пуст — отмена.")
        return False
    folder = _ask("  folder", folder)
    description = _ask("  description", "")
    program_name = _ask("  program_name", "")
    author = await _pick_author(db)

    program_id = await db.next_program_id()
    print(f"\n  Итог: program_id={program_id}  service_name={service_name}  folder={folder or 'NULL'}")
    print(f"        description={description or 'NULL'}  program_name={program_name or 'NULL'}  "
          f"author={author or 'NULL'}")
    print("        (флаги status/dispatcher/… = false, боты/cookies = NULL — дозаполнить отдельно)")
    if _ask("  Создать запись? [y/N]", "N").lower() != "y":
        print("  Отменено.")
        return False
    await db.create_program(program_id, service_name, folder or None,
                            description or None, program_name or None, author)
    print(f"  ✅ Создана запись program_id={program_id}.")
    return True
