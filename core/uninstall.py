"""Деинсталляция программы с ноды — строго свои service_name (без glob, чужое не трогаем).

Порядок: снять привязку в service_status (диспетчер перестаёт учитывать ноду) →
stop/disable/rm юнита (sudo) → daemon-reload → опц. rm папки проекта. Деструктивно —
с подтверждением; leader-нода — с жёстким предупреждением.
"""
import os
import shlex

from core import ui
from core.validate import list_local_services
from database import Database
from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)


async def uninstall(ssh: SshClient, db: Database, project_dir: str, preselect: str | None = None) -> None:
    records = await db.find_programs_by_service(
        [s.name for s in list_local_services(project_dir) if not s.is_template])
    if not records:
        print("Программы проекта не найдены в programdata.")
        return

    print("\nПрограммы проекта:")
    for i, r in enumerate(records, 1):
        print(f"  [{i}] {r['service_name']}  (folder={r['folder']})")
    sel = await ui.ask("Какую деинсталлировать (номер)", "1")
    if not (sel.isdigit() and 1 <= int(sel) <= len(records)):
        print("Неверный выбор.")
        return
    rec = records[int(sel) - 1]
    unit = rec["service_name"]
    folder = (rec["folder"] or "").rstrip("/")

    bindings = await db.get_service_bindings(rec["program_id"])
    if not bindings:
        print("У программы нет привязок к нодам — деинсталлировать нечего.")
        return
    labels = [f"{(b['server_name'] or b['ip_address']):16} [{b['status']}] running={b['running']}"
              for b in bindings]
    if preselect:
        idxs = (list(range(len(bindings))) if preselect.lower() == "all"
                else [int(t) - 1 for t in preselect.replace(" ", "").split(",")
                      if t.isdigit() and 1 <= int(t) <= len(bindings)])
    else:
        idxs = await ui.checkbox(f"Ноды для деинсталляции {unit}:", labels)
    chosen = [bindings[i] for i in idxs]
    if not chosen:
        print("Ноды не выбраны.")
        return

    rm_folder = (await ui.confirm(f"Удалять также папку проекта на ноде (rm -rf {folder or '—'})?")) if folder else False
    unit_path = shlex.quote(os.path.join(config.SYSTEMD_DIR, unit))

    for b in chosen:
        node = b["server_name"] or b["ip_address"]
        ip = b["ip_address"]
        if b["status"] == "leader":
            print(f"  ⚠️⚠️ {node} — АКТИВНЫЙ leader для {unit}!")
        if not await ui.confirm(f"  Деинсталлировать {unit} с {node}"
                                f"{' + удалить папку' if rm_folder else ''}? Необратимо."):
            print(f"  ⏭️  {node} пропущен")
            continue

        # 1) снять привязку (диспетчер перестаёт учитывать ноду — без failover-гонки)
        await db.unbind_service_node(rec["program_id"], b["node_id"])
        # 2) стоп/disable/удаление юнита (через ';' — не падаем, если не запущен/не enabled)
        inner = (f"systemctl stop {shlex.quote(unit)}; systemctl disable {shlex.quote(unit)}; "
                 f"rm -f {unit_path}; systemctl daemon-reload")
        res = await ssh.run(ip, f"sh -c {shlex.quote(inner)}", sudo=True, timeout=60)
        # 3) опц. удаление папки (под vova — sudo не нужен)
        folder_msg = ""
        if rm_folder and folder:
            fr = await ssh.run(ip, f"rm -rf {shlex.quote(folder)}", timeout=60)
            folder_msg = "  папка удалена" if fr.ok else f"  папка НЕ удалена: {fr.stderr or fr.stdout}"
        ok = res.ok or "not loaded" in (res.stderr or "")  # systemctl мог ругнуться на отсутствующий юнит
        print(f"  {'✅' if ok else '❌'} {node}: привязка снята; "
              f"{'юнит остановлен/удалён' if res.ok else 'systemctl: ' + (res.stderr or res.stdout)[:120]}{folder_msg}")

    remaining = await db.get_service_bindings(rec["program_id"])
    parts = [f"{b['server_name'] or b['ip_address']}[{b['status']}]" for b in remaining]
    print(f"\nОсталось привязок у {unit}: {', '.join(parts) if parts else '— нет'}")
