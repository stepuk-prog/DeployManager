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

    idx = await ui.select("Программа", [r["service_name"] for r in records])
    if idx is None:
        print("Программа не выбрана.")
        return
    rec = records[idx]

    bindings = await db.get_service_bindings(rec["program_id"])
    if not bindings:
        print("У программы нет привязок к нодам (dispatcher.service_status).")
        return
    labels = [f"{(b['server_name'] or b['ip_address']):16} rang={b['rang']!s:>4} "
              f"[{b['status']}] running={b['running']}"
              for b in bindings]
    if preselect:
        idxs = (list(range(len(bindings))) if preselect.lower() == "all"
                else [int(t) - 1 for t in preselect.replace(" ", "").split(",")
                      if t.isdigit() and 1 <= int(t) <= len(bindings)])
    else:
        idxs = await ui.checkbox(f"Ноды для {rec['service_name']}:", labels)
    chosen = [bindings[i] for i in idxs]
    if not chosen:
        print("Ноды не выбраны.")
        return

    if not command:
        ci = await ui.select("Команда", list(ALLOWED), default_index=ALLOWED.index("restart"))
        if ci is None:
            print("Команда не выбрана.")
            return
        command = ALLOWED[ci]
    if command not in ALLOWED:
        print(f"Команда должна быть из {ALLOWED}.")
        return

    for b in chosen:
        node = b["server_name"] or b["ip_address"]
        if command in ("stop", "restart") and b["status"] == "leader":
            if not await ui.confirm(f"  ⚠️ {node} — leader. '{command}' остановит/перезапустит "
                                    f"РАБОТАЮЩИЙ сервис. Продолжить?"):
                print(f"  ⏭️  {node} пропущен")
                continue
        # событие в service_error_log → его id в log_id инструкции (иначе агент падает на
        # error_handling_log.error_log_id NOT NULL после успешного выполнения).
        log_id = await db.insert_dm_event(rec["program_id"], b["node_id"], command)
        iid = await db.queue_instruction(rec["service_name"], command, b["node_id"],
                                         source="dm", log_id=log_id)
        print(f"  ✓ #{iid}: {command} {rec['service_name']} @ {node} — в очереди watchdog")
        print(f"      → {await _poll(db, iid, poll_timeout)}")