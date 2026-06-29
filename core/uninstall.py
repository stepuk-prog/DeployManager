"""Деинсталляция — строго свои service_name (без glob, чужое не трогаем).

Модель: проект = общая папка + НАБОР юнитов. Режимы поиска:
  [1] по service-файлам проекта → деинсталлируем ВЕСЬ проект (все его юниты + папку);
  [2] из БД → одна программа (для старых одиночных), все из programdata + пометки.
Гейт: если хоть один юнит активен (status=true) — запрет (сперва выключить через watchdog).
Цели на нодах — SSH-проба наличия папки/юнитов. Чистка: снять привязки → stop/disable/rm
юнитов (root) → daemon-reload → опц. rm папки → журнал. В конце — опц. удаление записей
из programdata (каскадно).
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


async def _pick_one(progs: list, prompt: str):
    """Выбор одной программы из списка (combobox по program_name) → ([rec], folder)."""
    if not progs:
        print("Ничего не найдено.")
        return None, None
    labels = [f"{p['program_name'] or p['service_name'] or '?'}  ·  {p['service_name'] or '?'}  "
              f"[{'АКТИВНА' if p['status'] else 'выкл'}, disp={'on' if p['dispatcher'] else 'off'}]"
              for p in progs]
    idx = await ui.combobox(prompt, labels)
    if idx is None:
        return None, None
    rec = progs[idx]
    return [rec], (rec["folder"] or "").rstrip("/")


async def _pick_targets(db: Database, project_dir: str):
    """Возвращает (список программ, общая папка) либо (None, None)."""
    mode = await ui.select("Как искать программу", [
        "по service-файлам проекта (весь проект)",
        "из БД (одна программа)",
        "из журнала деплоя (что ставили этим инструментом)",
    ])
    if mode is None:
        return None, None
    if mode == 1:                                       # из БД — все записи programdata
        return await _pick_one(await db.list_programs(), "Программа для деинсталляции (по имени):")
    if mode == 2:                                       # из журнала — только что деплоили
        return await _pick_one(await db.journal_programs(), "Программа из журнала деплоя:")

    records = await db.find_programs_by_service(
        [s.name for s in list_local_services(project_dir) if not s.is_template])
    if not records:
        print("Программы проекта не найдены в programdata.")
        return None, None
    folders = {(r["folder"] or "").rstrip("/") for r in records if r["folder"]}
    folder = folders.pop() if len(folders) == 1 else (sorted(folders)[0] if folders else "")
    if len(folders) > 1:
        print(f"⚠️ У юнитов проекта разные папки: {folders} — беру {folder}")
    print(f"\nПроект: {len(records)} юнит(ов), общая папка {folder or '?'}:")
    for r in records:
        print(f"  • {r['service_name']}  {'(АКТИВНА)' if r['status'] else ''}")
    return records, folder


async def uninstall(ssh: SshClient, db: Database, project_dir: str, preselect: str | None = None) -> None:
    programs, folder = await _pick_targets(db, project_dir)
    if not programs:
        return

    active = [p["service_name"] for p in programs if p["status"]]
    if active:
        print(f"🛑 Активны (status=true): {', '.join(active)}. Деинсталляция запрещена.")
        print("   Сначала выключи их через watchdog (status снимется), затем повтори.")
        return

    binds = {p["program_id"]: {b["node_id"]: b for b in await db.get_service_bindings(p["program_id"])}
             for p in programs}
    units = {p["service_name"]: os.path.join(config.SYSTEMD_DIR, p["service_name"]) for p in programs}

    print("\nИщу проект на серверах (проба наличия)…")
    nodes = await db.get_online_nodes()
    cands = []   # (node, has_folder, [present_units])
    for n in nodes:
        ip = n["ip_address"]
        bound_here = any(n["id"] in binds[pid] for pid in binds)
        hf, present = False, []
        if await ssh.ping(ip):                          # недоступную не зондируем (иначе таймауты)
            try:
                hf = await ssh.path_exists(ip, folder) if folder else False
                present = [u for u in units if await ssh.path_exists(ip, units[u])]
            except Exception as e:                      # noqa: BLE001 — одна нода не валит весь поиск
                print(f"  ⚠️ {n['server_name'] or ip}: ошибка пробы ({e}) — пропускаю")
                continue
        elif not bound_here:
            continue                                    # offline и без привязки — нечего чистить
        if hf or present or bound_here:
            cands.append((n, hf, present))
    if not cands:
        print("Нигде не найдено: ни папки, ни юнитов, ни привязок.")
    else:
        labels = []
        for n, hf, present in cands:
            marks = (["папка"] if hf else []) + ([f"{len(present)} юнит."] if present else [])
            if any(n["id"] in binds[pid] for pid in binds):
                marks.append("привязки")
            labels.append(f"{(n['server_name'] or n['ip_address']):16} {n['ip_address']:16} [{', '.join(marks)}]")
        if preselect:
            idxs = (list(range(len(cands))) if preselect.lower() == "all"
                    else [int(t) - 1 for t in preselect.replace(" ", "").split(",")
                          if t.isdigit() and 1 <= int(t) <= len(cands)])
        else:
            idxs = await ui.checkbox("Откуда деинсталлировать проект:", labels)
        chosen = [cands[i] for i in idxs]
        rm_folder = (await ui.confirm(f"Удалять также папку проекта (rm -rf {folder or '—'})?")) if folder else False
        operator = getpass.getuser()

        for n, hf, present in chosen:
            node = n["server_name"] or n["ip_address"]
            ip = n["ip_address"]
            if not await ui.confirm(f"  Деинсталлировать {len(programs)} юнит(ов) с {node}"
                                    f"{' + удалить папку' if rm_folder else ''}? Необратимо."):
                print(f"  ⏭️  {node} пропущен")
                continue
            # остановить/выключить/удалить все юниты одним заходом (root)
            stops = []
            for p in programs:
                u = shlex.quote(p["service_name"])
                stops.append(f"systemctl stop {u}; systemctl disable {u}; rm -f {shlex.quote(units[p['service_name']])}")
            inner = "; ".join(stops + ["systemctl daemon-reload"])
            res = await ssh.run_priv(ip, f"sh -c {shlex.quote(inner)}", timeout=90)
            if res.ok:                                  # привязку в БД снимаем ТОЛЬКО после успеха на ноде
                for p in programs:                      # (иначе рассинхрон: в БД снято, а юнит жив)
                    if n["id"] in binds[p["program_id"]]:
                        await db.unbind_service_node(p["program_id"], n["id"])
            folder_msg = ""
            if rm_folder and folder:
                fr = await ssh.run(ip, f"rm -rf {shlex.quote(folder)}", timeout=60)
                folder_msg = "  папка удалена" if fr.ok else f"  папка НЕ удалена: {fr.stderr or fr.stdout}"
            print(f"  {'✅' if res.ok else '❌'} {node}: юниты остановлены/удалены"
                  f"{'' if res.ok else ' (priv: ' + (res.stderr or res.stdout)[:100] + ')'}{folder_msg}")
            for p in programs:
                await db.journal_write(p["program_id"], n["id"], "uninstall",
                                       folder_deployed=bool(folder and hf and not rm_folder),
                                       service_installed=False, db_updated=False,
                                       result=("ok" if res.ok else "fail"), commit=None, operator=operator,
                                       details={"node": node, "rm_folder": rm_folder})

    # опц. удаление записей из programdata (каскадно)
    if await ui.confirm(
        f"Удалить {len(programs)} запис(ь/и) проекта из programdata?\n"
        "Каскадное удаление сотрёт ВСЮ информацию о данной программе (привязки, "
        "настройки, логи и т.д.). Действие НЕОБРАТИМО.",
        danger=True):
        names = ", ".join(p.get("service_name") or str(p["program_id"]) for p in programs)
        if not await ui.confirm(
            f"⚠️⚠️ Подтвердите окончательно: удалить из programdata ({len(programs)}):\n"
            f"{names}\n"
            "Это последнее предупреждение — записи будут стёрты безвозвратно.",
            danger=True):
            print("Записи в programdata оставлены.")
            return
        for p in programs:
            await db.delete_program(p["program_id"])
            await db.journal_write(p["program_id"], None, "uninstall", False, False, True,
                                   result="programdata deleted", commit=None,
                                   operator=getpass.getuser(), details={"deleted_programdata": True})
        print(f"✅ Удалено записей: {len(programs)}.")
    else:
        print("Записи в programdata оставлены.")
