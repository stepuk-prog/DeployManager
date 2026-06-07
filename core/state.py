"""State-check: фактическое состояние сервисов на нодах (systemctl) → service_status.

По каждой привязке (dispatcher.service_status) опрашиваем `systemctl show` (без sudo),
определяем running и текст ошибки, пишем в running/systemd_error/last_running_update.
Старт/стоп/проверку как управление сервисом делает диспетчер — здесь только снимок состояния.
"""
import asyncio
import shlex
from dataclasses import dataclass

from classes.ssh_client import SshClient
from core import ui
from core.validate import list_local_services
from database import Database
from logs import get_logger

logger = get_logger(__name__)


@dataclass
class UnitState:
    running: bool
    error: str | None
    load: str
    active: str


def parse_systemctl_show(stdout: str) -> UnitState:
    """Разбор `systemctl show -p LoadState -p ActiveState -p SubState -p Result`."""
    props = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    if load != "loaded":
        return UnitState(False, f"unit {load or 'unknown'}", load, active)
    running = active == "active"
    error = f"{props.get('SubState', '')}/{props.get('Result', '')}" if active == "failed" else None
    return UnitState(running, error, load, active)


async def _unit_state(ssh: SshClient, host: str, unit: str) -> UnitState | None:
    """Состояние юнита на ноде (None — нода недоступна по SSH)."""
    res = await ssh.run(
        host, f"systemctl show {shlex.quote(unit)} -p LoadState -p ActiveState -p SubState -p Result",
        timeout=15)
    if not res.ok and not res.stdout:
        return None
    return parse_systemctl_show(res.stdout)


async def check_state(ssh: SshClient, db: Database, project_dir: str) -> None:
    """Опросить состояние сервисов проекта по их привязкам и записать в service_status."""
    svcs = list_local_services(project_dir)
    names = [s.name for s in svcs if not s.is_template]
    records = await db.find_programs_by_service(names)
    if not records:
        print("Программы проекта не найдены в programdata.")
        return
    print("\n══ Проверка состояния сервисов (systemctl → service_status) ══")
    ui.progress("Опрос состояния сервисов на нодах…")
    for rec in records:
        unit = rec["service_name"]
        bindings = await db.get_service_bindings(rec["program_id"])
        print(f"\n▸ {unit}")
        if not bindings:
            print("    — нет привязок")
            continue
        states = await asyncio.gather(*[_unit_state(ssh, b["ip_address"], unit) for b in bindings])
        for b, st in zip(bindings, states):
            ip = b["ip_address"]
            node = b["server_name"] or ip
            if st is None:
                print(f"    {node:16} 🔌 недоступна — пропускаю (БД не трогаю)")
                continue
            await db.update_service_state(rec["program_id"], b["node_id"], st.running, st.error)
            icon = "▶ active" if st.running else (f"✗ {st.active}" if st.active else "■ inactive")
            tail = f"  ошибка: {st.error}" if st.error else ""
            print(f"    {node:16} {b['status']:11} {icon}{tail}")
    ui.progress("")