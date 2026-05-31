"""Деинсталляция программы с ноды — строго свои service_name (без glob, чужое не трогаем).

Где искать цели: SSH-проба online-нод на наличие папки/юнита (ground truth — ловит и
«хвосты» неудачного деплоя без привязки), + пометка привязки и последнего журнала.
Порядок: снять привязку (если есть) → stop/disable/rm юнита (root) → daemon-reload →
опц. rm папки → запись в журнал. Деструктивно; leader — с жёстким предупреждением.
"""
import getpass
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
    unit_etc = os.path.join(config.SYSTEMD_DIR, unit)

    nodes = await db.get_online_nodes()
    bound = {b["node_id"]: b for b in await db.get_service_bindings(rec["program_id"])}

    print(f"\nИщу '{unit}' на серверах (проба наличия)…")
    cands = []  # (node, has_folder, has_unit)
    for n in nodes:
        ip = n["ip_address"]
        has_folder = await ssh.path_exists(ip, folder) if folder else False
        has_unit = await ssh.path_exists(ip, unit_etc)
        if has_folder or has_unit or n["id"] in bound:
            cands.append((n, has_folder, has_unit))
    if not cands:
        print("Нигде не найдено: ни папки, ни юнита, ни привязки. Удалять нечего.")
        return

    labels = []
    for n, hf, hu in cands:
        marks = []
        if hf:
            marks.append("папка")
        if hu:
            marks.append("юнит")
        if n["id"] in bound:
            marks.append(f"привязка:{bound[n['id']]['status']}")
        labels.append(f"{(n['server_name'] or n['ip_address']):16} {n['ip_address']:16} "
                      f"[{', '.join(marks)}]")
    if preselect:
        idxs = (list(range(len(cands))) if preselect.lower() == "all"
                else [int(t) - 1 for t in preselect.replace(" ", "").split(",")
                      if t.isdigit() and 1 <= int(t) <= len(cands)])
    else:
        idxs = await ui.checkbox(f"Откуда деинсталлировать {unit}:", labels)
    chosen = [cands[i] for i in idxs]
    if not chosen:
        print("Ноды не выбраны.")
        return

    rm_folder = (await ui.confirm(f"Удалять также папку проекта (rm -rf {folder or '—'})?")) if folder else False
    operator = getpass.getuser()

    for n, hf, hu in chosen:
        node = n["server_name"] or n["ip_address"]
        ip = n["ip_address"]
        b = bound.get(n["id"])
        if b and b["status"] == "leader":
            print(f"  ⚠️⚠️ {node} — АКТИВНЫЙ leader для {unit}!")
        if not await ui.confirm(f"  Деинсталлировать {unit} с {node}"
                                f"{' + удалить папку' if rm_folder else ''}? Необратимо."):
            print(f"  ⏭️  {node} пропущен")
            continue

        if b is not None:
            await db.unbind_service_node(rec["program_id"], n["id"])
        inner = (f"systemctl stop {shlex.quote(unit)}; systemctl disable {shlex.quote(unit)}; "
                 f"rm -f {shlex.quote(unit_etc)}; systemctl daemon-reload")
        res = await ssh.run_priv(ip, f"sh -c {shlex.quote(inner)}", timeout=60)
        folder_msg = ""
        if rm_folder and folder:
            fr = await ssh.run(ip, f"rm -rf {shlex.quote(folder)}", timeout=60)
            folder_msg = "  папка удалена" if fr.ok else f"  папка НЕ удалена: {fr.stderr or fr.stdout}"
        print(f"  {'✅' if res.ok else '❌'} {node}: "
              f"{'привязка снята; ' if b is not None else ''}"
              f"{'юнит остановлен/удалён' if res.ok else 'priv: ' + (res.stderr or res.stdout)[:120]}{folder_msg}")
        await db.journal_write(
            rec["program_id"], n["id"], "uninstall",
            folder_deployed=bool(folder and hf and not rm_folder),
            service_installed=False, db_updated=False,
            result=("ok" if res.ok else "fail"), commit=None, operator=operator,
            details={"had_folder": hf, "had_unit": hu, "rm_folder": rm_folder})

    remaining = await db.get_service_bindings(rec["program_id"])
    parts = [f"{x['server_name'] or x['ip_address']}[{x['status']}]" for x in remaining]
    print(f"\nОсталось привязок у {unit}: {', '.join(parts) if parts else '— нет'}")