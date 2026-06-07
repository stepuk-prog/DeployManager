"""Интерактивное создание записи program.programdata — пошаговый мастер.

Источники данных (править руками нечего, кроме description/program_name/автора):
  • service_name — имя service-файла проекта;
  • folder       — WorkingDirectory из этого юнита;
  • program_name — параметр PROG_NAME из {проект}/.env (если нет — спросим отдельным окном);
  • description  — ввод (необязателен);
  • author       — выбор одного из известных (Proger M1 / Толстый);
  • dispatcher   — оператор решает, вести ли программу диспетчером.
program_id = MAX+1; status — всегда false; FK ботов/cookies — NULL.
"""
import os

from core import ui
from database import Database
from logs import get_logger

logger = get_logger(__name__)

# Известные авторы записей programdata (id телеграма → отображаемое имя). См. CLAUDE.md.
_AUTHORS: list[tuple[int, str]] = [
    (975218672, "Proger M1"),
    (6275724296, "Толстый"),
]

_CONTINUE = "✅ Продолжить"
_CANCEL = "✖️ Отмена"


def _read_prog_name(project_dir: str) -> str | None:
    """PROG_NAME из {project_dir}/.env без побочных эффектов на os.environ (свой мини-парсер)."""
    path = os.path.join(project_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == "PROG_NAME":
                    return val.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


async def create_record_interactive(db: Database, project_dir: str,
                                    service_name: str, folder: str | None) -> bool:
    """Мастер создания записи programdata. Отмена на любом шаге → запись не создаётся."""
    prog_name = _read_prog_name(project_dir)

    # Окно 1 — имеющиеся (производные) данные; program_name показываем только если он есть в .env.
    lines = ["Имеющиеся данные:",
             f"service_name — {service_name}",
             f"folder — {folder or '—'}"]
    if prog_name:
        lines.append(f"program_name — {prog_name}")
    if not await ui.confirm("\n".join(lines), ok_label=_CONTINUE, cancel_label=_CANCEL):
        print("  Отменено.")
        return False

    # Окно 2 — program_name, только если в .env его нет.
    if not prog_name:
        ans = await ui.ask("Имя программы (program_name):", "", cancelable=True,
                           ok_label=_CONTINUE, cancel_label=_CANCEL)
        if ans is None:
            print("  Отменено.")
            return False
        prog_name = ans or None

    # Окно 3 — description (необязателен: пусто → NULL).
    desc = await ui.ask("Описание (description, необязательно):", "", cancelable=True,
                        ok_label=_CONTINUE, cancel_label=_CANCEL)
    if desc is None:
        print("  Отменено.")
        return False
    description = desc or None

    # Окно 4 — автор (ровно один, радиокнопки).
    idx = await ui.radio("Автор записи:", [name for _, name in _AUTHORS], default_index=0)
    if idx is None:
        print("  Отменено.")
        return False
    author = _AUTHORS[idx][0]

    # Окно 5 — вести ли программу диспетчером.
    dispatcher = await ui.confirm(
        f"Включить управление диспетчером для «{service_name}»?")

    program_id = await db.create_program(service_name, folder or None,
                                         description, prog_name, author, dispatcher=dispatcher)
    print(f"  ✅ Создана запись program_id={program_id} "
          f"(program_name={prog_name or 'NULL'}, dispatcher={'вкл' if dispatcher else 'выкл'}).")
    return True
