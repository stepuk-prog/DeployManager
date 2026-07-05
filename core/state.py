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
        if not bindings:
            print(f"  {unit}   — нет привязок")
            continue
        states = await asyncio.gather(*[_unit_state(ssh, b["ip_address"], unit) for b in bindings])
        for b, st in zip(bindings, states):            # снимок состояния в БД (кроме недоступных нод)
            if st is not None:
                await db.update_service_state(rec["program_id"], b["node_id"], st.running, st.error)
        print(f"  {unit}   {_summarize(bindings, states)}")
    ui.progress("")


def _summarize(bindings: list, states: list) -> str:
    """Одна строка на сервис: кто leader + агрегат состояния; поимённо — только отклонения
    (active/failed/offline). Однородная масса сворачивается в «все остановлены»/«все active»."""
    leaders, active, stopped, failed, offline = [], [], [], [], []
    for b, st in zip(bindings, states):
        node = b["server_name"] or b["ip_address"]
        if b["status"] == "leader":
            leaders.append(node)
        if st is None:
            offline.append(node)
        elif st.error:
            failed.append((node, st.error))
        elif st.running:
            active.append(node)
        else:
            stopped.append(node)
    segs = [f"leader {', '.join(leaders)}" if leaders else "без leader"]
    probed = len(active) + len(stopped) + len(failed)      # ноды, ответившие по SSH
    if active and not stopped:
        segs.append("все active" if probed and len(active) == probed else f"▶ active: {', '.join(active)}")
    elif active and stopped:
        segs.append(f"▶ active: {', '.join(active)} · остальные остановлены")
    elif stopped or failed:
        segs.append("все остановлены")
    elif offline and not active:
        segs.append("🔌 все недоступны")
    for node, err in failed:
        segs.append(f"✗ {node} {err}")
    if offline and (active or stopped or failed):
        segs.append(f"🔌 offline: {', '.join(offline)}")
    return " · ".join(segs)