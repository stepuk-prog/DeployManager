"""Управление сервисами через GlobalDispatcher (§13): намерение в dispatcher.control_request.

DeployManager НЕ ставит raw watchdog_instruction и НЕ дёргает systemctl — подаёт НАМЕРЕНИЕ
(start/stop/restart) в control_request, а GD подбирает placement (start — лучшая нода по rang;
stop/restart — лидер) и исполняет через lifecycle с honest-verify. Размещение — за GD, поэтому
конкретную ноду НЕ выбираем (привязки показываем как инфо). Перед подачей включаем диспетчера
(programdata.dispatcher=true): GD управляет ТОЛЬКО dispatcher=true (иначе терминал NonDispatcher).
"""
import asyncio

from core import ui
from core.validate import list_local_services
from database import Database
from logs import get_logger

logger = get_logger(__name__)

ALLOWED = ("start", "stop", "restart")

# Поллинг исхода control_request у GD (lifecycle + verify может занять десятки секунд).
_POLL_TIMEOUT = 90
_POLL_INTERVAL = 2


def _print_terminal(command: str, service_name: str, row) -> None:
    """Печать терминального исхода намерения GD."""
    st = row["req_status"]
    if st == "completed":
        node = row["actual_node_id"]
        node_txt = f" (node {node})" if node is not None else ""
        print(f"      ✅ {command} {service_name} выполнено и верифицировано GD{node_txt}.")
    elif st == "failed":
        detail = row["completion_result"] or row["instr_status"] or "см. логи GD"
        print(f"      ❌ {command} {service_name}: {detail}.")
    elif st == "NonDispatcher":
        print(f"      ⚠️ {command} {service_name}: программа вне власти GD (dispatcher=false). "
              f"{row['decided_reason'] or ''}".rstrip())
    else:  # cancelled
        print(f"      ⚠️ GD отклонил {command} {service_name}: {row['decided_reason'] or '—'}")


async def _poll_outcome(db: Database, request_id: int, command: str, service_name: str,
                        timeout: int = _POLL_TIMEOUT) -> None:
    """Подождать терминала control_request у GD (poll БД) + напечатать исход."""
    waited = 0
    last_status = "pending"
    while waited < timeout:
        row = await db.poll_request(request_id)
        if row is None:
            print(f"      ❌ control_request#{request_id} исчез.")
            return
        last_status = row["req_status"]
        if last_status in ("completed", "failed", "cancelled", "NonDispatcher"):
            _print_terminal(command, service_name, row)
            return
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
    print(f"      ⏱ GD не довёл {command} {service_name} за {timeout}с "
          f"(control_request#{request_id} ещё '{last_status}'). GD запущен и есть лидер?")


async def manage(db: Database, project_dir: str, command: str | None = None,
                 poll_timeout: int = _POLL_TIMEOUT) -> None:
    """Подать команду сервису через GlobalDispatcher (§13). Размещение — за GD."""
    local_svcs = [s for s in list_local_services(project_dir) if not s.is_template]
    records = await db.find_programs_by_service([s.name for s in local_svcs])
    if not records:
        print("Программы проекта не найдены в programdata.")
        return

    # порции по каталогам systemd/ (OTC/Binary/Crypto…): при >1 каталоге сперва сужаем до каталога
    group_by_name = {s.name: s.group for s in local_svcs}
    groups: dict[str, list] = {}
    for r in records:
        groups.setdefault(group_by_name.get(r["service_name"], ""), []).append(r)
    recs = records
    if len(groups) > 1:
        gnames = sorted(groups, key=lambda g: (g == "", g))   # корень ('') — в конец
        gi = await ui.select("Каталог-порция systemd/",
                             [f"{(g or '(корень)')}  ({len(groups[g])} шт.)" for g in gnames])
        if gi is None:
            print("Каталог не выбран.")
            return
        recs = groups[gnames[gi]]

    idx = await ui.select("Программа", [r["service_name"] for r in recs])
    if idx is None:
        print("Программа не выбрана.")
        return
    rec = recs[idx]

    # Привязки — ТОЛЬКО как инфо (размещение решает GD, ноду не выбираем).
    bindings = await db.get_service_bindings(rec["program_id"])
    if bindings:
        print(f"  Текущее размещение {rec['service_name']} (выбор ноды — за GD):")
        for b in bindings:
            node = b["server_name"] or b["ip_address"]
            print(f"    • {node:16} rang={b['rang']!s:>4} [{b['status']}] running={b['running']}")
    else:
        print("  У программы нет привязок к нодам — GD разместит на доступном кандидате.")

    if not command:
        ci = await ui.select("Команда", list(ALLOWED), default_index=ALLOWED.index("restart"))
        if ci is None:
            print("Команда не выбрана.")
            return
        command = ALLOWED[ci]
    if command not in ALLOWED:
        print(f"Команда должна быть из {ALLOWED}.")
        return

    if command in ("stop", "restart") and any(b["status"] == "leader" for b in bindings):
        if not await ui.confirm(f"  ⚠️ '{command}' через GD затронет РАБОТАЮЩИЙ сервис "
                                f"{rec['service_name']} (на лидере). Продолжить?"):
            print("  ⏭️  Отменено.")
            return

    # Включить диспетчера: GD управляет только dispatcher=true (иначе NonDispatcher).
    enabled = await db.enable_dispatcher(rec["program_id"])
    if enabled is not None:
        print(f"  🧰 Диспетчер включён для {rec['service_name']} (был выключен) — отдаю под GD.")

    req_id = await db.submit_control_request(
        rec["program_id"], rec["service_name"], command, source="dm",
    )
    print(f"  ✓ control_request#{req_id}: {command} {rec['service_name']} → GD (source=dm). "
          f"Жду исполнения…")
    await _poll_outcome(db, req_id, command, rec["service_name"], poll_timeout)
