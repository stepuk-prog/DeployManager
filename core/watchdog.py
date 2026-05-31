"""Управление сервисами через диспетчер: ставим инструкцию в dispatcher.watchdog_instruction.

Сами systemctl НЕ дёргаем — команду (start/stop/restart) исполняет watchdog-агент на ноде
(он же пишет executed_at/result). Это штатный путь, не конфликтующий с failover диспетчера.
stop/restart на активном leader — с предупреждением.
"""
import asyncio

from core import ui
from core.validate import list_local_services
from database import Database
from logs import get_logger

logger = get_logger(__name__)

ALLOWED = ("start", "stop", "restart")


async def _poll(db: Database, instruction_id: int, timeout: int = 60) -> str:
    """Подождать исполнения инструкции watchdog'ом (poll БД)."""
    waited = 0
    while waited < timeout:
        row = await db.get_instruction(instruction_id)
        if row and row["is_executed"]:
            return f"выполнено: {row['result']} ({row['executed_at']})"
        await asyncio.sleep(2)
        waited += 2
    return f"ещё не выполнено за {timeout}с — watchdog подхватит позже"


async def manage(db: Database, project_dir: str, command: str | None = None,
                 preselect: str | None = None, poll_timeout: int = 60) -> None:
    """Поставить команду сервису на выбранных нодах через watchdog."""
    records = await db.find_programs_by_service(
        [s.name for s in list_local_services(project_dir) if not s.is_template])
    if not records:
        print("Программы проекта не найдены в programdata.")
        return

    print("\nПрограммы проекта:")
    for i, r in enumerate(records, 1):
        print(f"  [{i}] {r['service_name']}")
    sel = ui.ask("Программа (номер)", "1")
    if not (sel.isdigit() and 1 <= int(sel) <= len(records)):
        print("Неверный выбор программы.")
        return
    rec = records[int(sel) - 1]

    bindings = await db.get_service_bindings(rec["program_id"])
    if not bindings:
        print("У программы нет привязок к нодам (dispatcher.service_status).")
        return
    print(f"\nНоды для {rec['service_name']}:")
    for i, b in enumerate(bindings, 1):
        print(f"  [{i}] {(b['server_name'] or b['ip_address']):16} [{b['status']}] "
              f"running={b['running']}")
    raw = preselect or ui.ask("Ноды (номера через запятую / 'all')", "all")
    if raw.lower() == "all":
        chosen = bindings
    else:
        chosen = [bindings[int(t) - 1] for t in raw.replace(" ", "").split(",")
                  if t.isdigit() and 1 <= int(t) <= len(bindings)]
    if not chosen:
        print("Ноды не выбраны.")
        return

    command = command or ui.ask(f"Команда {'/'.join(ALLOWED)}", "restart")
    if command not in ALLOWED:
        print(f"Команда должна быть из {ALLOWED}.")
        return

    for b in chosen:
        node = b["server_name"] or b["ip_address"]
        if command in ("stop", "restart") and b["status"] == "leader":
            if not ui.confirm(f"  ⚠️ {node} — leader. '{command}' остановит/перезапустит РАБОТАЮЩИЙ "
                              f"сервис. Продолжить?"):
                print(f"  ⏭️  {node} пропущен")
                continue
        iid = await db.queue_instruction(rec["service_name"], command, b["node_id"], source="dm")
        print(f"  ✓ #{iid}: {command} {rec['service_name']} @ {node} — в очереди watchdog")
        print(f"      → {await _poll(db, iid, poll_timeout)}")