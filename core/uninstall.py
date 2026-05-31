"""Деинсталляция программы — строго свои service_name (без glob, чужое не трогаем).

Поиск программы — 2 режима:
  [1] по service-файлам проекта (текущий проект);
  [2] из БД — все программы programdata (в т.ч. старые) + пометки статуса/привязок.
Гейт: активную (status=true) деинсталлировать нельзя — сперва выключить через watchdog.
Цели на нодах ищем SSH-пробой наличия папки/юнита (ground truth, ловит «хвосты» провалов).
Чистка: снять привязку → stop/disable/rm юнита (root) → daemon-reload → опц. rm папки →
журнал. В конце — отдельный вопрос про удаление записи из programdata (каскадно).
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


async def _pick_program(db: Database, project_dir: str):
    """Выбор программы: [1] по service-файлам проекта / [2] из БД (все, с пометками)."""
    mode = await ui.ask("Искать программу: [1] по service-файлам проекта / [2] из БД (все)", "1")
    if mode == "2":
        progs = await db.list_programs()
        if not progs:
            print("В programdata нет программ.")
            return None
        flt = (await ui.ask("Фильтр по имени (пусто — все)", "")).strip().lower()
        progs = [p for p in progs if not flt or flt in (p["service_name"] or "").lower()
                 or flt in (p["program_name"] or "").lower()]
        if not progs:
            print("Под фильтр ничего не подошло.")
            return None
        print("\nПрограммы (из programdata):")
        for i, p in enumerate(progs, 1):
            st = "АКТИВНА" if p["status"] else "выкл"
            print(f"  [{i}] {p['service_name'] or '?':26} {st:7} disp={'on' if p['dispatcher'] else 'off'}"
                  f"  {p['program_name'] or ''}")
        sel = await ui.ask("Программа (номер)", "1")
        if not (sel.isdigit() and 1 <= int(sel) <= len(progs)):
            print("Неверный выбор.")
            return None
        return progs[int(sel) - 1]

    # режим по service-файлам проекта
    records = await db.find_programs_by_service(
        [s.name for s in list_local_services(project_dir) if not s.is_template])
    if not records:
        print("Программы проекта не найдены в programdata.")
        return None
    print("\nПрограммы проекта:")
    for i, r in enumerate(records, 1):
        print(f"  [{i}] {r['service_name']}  (folder={r['folder']})")
    sel = await ui.ask("Какую деинсталлировать (номер)", "1")
    if not (sel.isdigit() and 1 <= int(sel) <= len(records)):
        print("Неверный выбор.")
        return None
    return records[int(sel) - 1]


async def uninstall(ssh: SshClient, db: Database, project_dir: str, preselect: str | None = None) -> None:
    rec = await _pick_program(db, project_dir)
    if rec is None:
        return
    unit = rec["service_name"]
    folder = (rec["folder"] or "").rstrip("/")
    unit_etc = os.path.join(config.SYSTEMD_DIR, unit)

    # гейт: активную программу деинсталлировать нельзя
    if rec["status"]:
        print(f"🛑 {unit}: status=true (активна). Деинсталляция запрещена.")
        print("   Сначала выключи программу через watchdog (он снимет status), затем повтори.")
        return

    nodes = await db.get_online_nodes()
    bound = {b["node_id"]: b for b in await db.get_service_bindings(rec["program_id"])}

    print(f"\nИщу '{unit}' на серверах (проба наличия)…")
    cands = []
    for n in nodes:
        ip = n["ip_address"]
        hf = await ssh.path_exists(ip, folder) if folder else False
        hu = await ssh.path_exists(ip, unit_etc)
        if hf or hu or n["id"] in bound:
            cands.append((n, hf, hu))
    if not cands:
        print("Нигде не найдено: ни папки, ни юнита, ни привязки.")
    else:
        labels = []
        for n, hf, hu in cands:
            marks = [m for m, on in (("папка", hf), ("юнит", hu)) if on]
            if n["id"] in bound:
                marks.append(f"привязка:{bound[n['id']]['status']}")
            labels.append(f"{(n['server_name'] or n['ip_address']):16} {n['ip_address']:16} [{', '.join(marks)}]")
        if preselect:
            idxs = (list(range(len(cands))) if preselect.lower() == "all"
                    else [int(t) - 1 for t in preselect.replace(" ", "").split(",")
                          if t.isdigit() and 1 <= int(t) <= len(cands)])
        else:
            idxs = await ui.checkbox(f"Откуда деинсталлировать {unit}:", labels)
        chosen = [cands[i] for i in idxs]
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
                  f"{'юнит удалён' if res.ok else 'priv: ' + (res.stderr or res.stdout)[:100]}{folder_msg}")
            await db.journal_write(rec["program_id"], n["id"], "uninstall",
                                   folder_deployed=bool(folder and hf and not rm_folder),
                                   service_installed=False, db_updated=False,
                                   result=("ok" if res.ok else "fail"), commit=None, operator=operator,
                                   details={"had_folder": hf, "had_unit": hu, "rm_folder": rm_folder})

    # отдельный вопрос: удалить запись из programdata (каскадно)
    remaining = await db.get_service_bindings(rec["program_id"])
    if remaining:
        parts = [f"{x['server_name'] or x['ip_address']}[{x['status']}]" for x in remaining]
        print(f"\n⚠️ Ещё остались привязки: {', '.join(parts)}")
        print("   При удалении записи они снимутся каскадно, но юниты на тех нодах останутся.")
    if await ui.confirm(f"Удалить запись программы {unit} из programdata (каскадно)?"):
        await db.delete_program(rec["program_id"])
        await db.journal_write(rec["program_id"], None, "uninstall", False, False, True,
                               result="programdata deleted", commit=None, operator=getpass.getuser(),
                               details={"deleted_programdata": True})
        print(f"✅ Запись {unit} (program_id={rec['program_id']}) удалена из programdata.")
    else:
        print("Запись в programdata оставлена.")
