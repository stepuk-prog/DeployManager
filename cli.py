"""CLI DeployManager (интерактивный + неинтерактивный через args).

Поток деплоя: папка проекта → git-версия → валидация (БД↔файлы) → предложение
доустановок → preflight (защита/добавление серверов) → rsync(.env+код) → provision →
юниты → VERSION → привязка standby + отчёт → хэш-сверка → статус → audit-лог.
"""
import getpass
import os
from datetime import datetime

from classes import Deployer, SshClient
from classes.manifest import local_version, parse_manifest
from core import audit as audit_mod
from core import deploy as deploy_mod
from core import provision as provision_mod
from core import status as status_mod
from core import ui
from core import validate as validate_mod
from core import verify as verify_mod
from database import Database
from logs import get_logger
from settings import config

logger = get_logger("cli")

_ACTION_MAP = {"deploy": "1", "status": "2", "create": "3"}


def _ask(prompt: str, default: str = "") -> str:
    return ui.ask(prompt, default)


def _parse_selection(nodes: list, raw: str) -> list:
    """Выбор нод по номерам / именам / ip / 'all'."""
    raw = (raw or "").strip()
    if raw.lower() == "all":
        return nodes
    out, seen = [], set()
    for tok in [t for t in raw.replace(" ", "").split(",") if t]:
        match = None
        if tok.isdigit() and 1 <= int(tok) <= len(nodes):
            match = nodes[int(tok) - 1]
        else:
            for n in nodes:
                if tok in (n["ip_address"], n["hostname"]) or tok.lower() == (n["server_name"] or "").lower():
                    match = n
                    break
        if match and match["ip_address"] not in seen:
            seen.add(match["ip_address"])
            out.append(match)
    return out


def _select_nodes(nodes: list, linked_ips: set[str], preselect: str | None = None) -> list:
    if preselect:
        return _parse_selection(nodes, preselect)
    print("\nДоступные online-ноды (* — связаны с программой в диспетчере):")
    for i, n in enumerate(nodes, 1):
        mark = "*" if n["ip_address"] in linked_ips else " "
        print(f"  [{i}]{mark} {(n['server_name'] or n['hostname']):18} "
              f"{n['ip_address']:16} {n['description'] or ''}")
    return _parse_selection(nodes, ui.ask("\nНоды для операции (напр. 1,3 / 'all')", "all"))


async def _resolve_remote_folder(db: Database, project_dir: str):
    """Путь установки (из programdata по service-файлам) + связанные ноды + записи."""
    local_svcs = validate_mod.list_local_services(project_dir)
    names = [s.name for s in local_svcs if not s.is_template]
    records = await db.find_programs_by_service(names)
    folders = {r["folder"].rstrip("/") for r in records if r["folder"]}
    linked_ips: set[str] = set()
    for r in records:
        for pn in await db.get_program_nodes(r["program_id"]):
            linked_ips.add(pn["ip_address"])
    if len(folders) == 1:
        return folders.pop(), local_svcs, linked_ips, records
    if not folders:
        return (_ask("Не нашёл программу в programdata. Путь установки вручную", "") or None), \
            local_svcs, linked_ips, records
    print(f"⚠️  В programdata разные пути: {folders}")
    return (_ask("Укажи путь установки вручную", sorted(folders)[0]) or None), \
        local_svcs, linked_ips, records


async def _preflight(ssh: SshClient, targets: list, remote_folder: str,
                     local, service_files: list[str]) -> list | None:
    """Предполётная проверка (первичный деплой / добавление серверов).

      • чисто (нет папки/юнитов) → новый сервер, деплоим;
      • есть наш VERSION → уже развёрнут → ПРОПУСКАЕМ (обновление — отдельная ветка);
      • папка/юнит без VERSION → чужое/частичное → спросить (перезаписать/пропустить/отмена).
    Если добавляем новые ноды, а уже развёрнутые на другой версии — предупреждаем о рассинхроне.
    Возвращает список одобренных нод, либо None — полная отмена.
    """
    print("\n── Предполётная проверка нод ──")
    version_path = f"{remote_folder.rstrip('/')}/{config.VERSION_FILE}"
    approved = []
    deployed: list[tuple[str, str]] = []
    for node in targets:
        ip = node["ip_address"]
        name = node["server_name"] or node["hostname"]
        if not await ssh.ping(ip):
            print(f"  {name:16} 🔌 недоступна — пропускаю")
            continue
        man = parse_manifest(await ssh.read_file(ip, version_path))
        if man is not None:
            short = man.get("short") or (man.get("commit", "")[:9])
            print(f"  {name:16} ↩️  уже развёрнут (v{short}) — обновление в отдельной ветке, пропускаю")
            deployed.append((name, man.get("commit", "")))
            continue
        folder_exists = await ssh.path_exists(ip, remote_folder)
        existing_units = [sf for sf in service_files
                          if await ssh.path_exists(ip, f"{config.SYSTEMD_DIR}/{sf}")]
        if not folder_exists and not existing_units:
            print(f"  {name:16} ✅ чистый деплой (новый сервер)")
            approved.append(node)
            continue
        print(f"  {name:16} ⚠️ найдено существующее (НЕ помечено нашим деплоем):")
        if folder_exists:
            print(f"        папка уже есть: {remote_folder}")
        if existing_units:
            print(f"        юниты уже установлены: {', '.join(existing_units)}")
        ans = ui.ask("        [o] перезаписать / [s] пропустить ноду / [a] отмена всего", "s").lower()
        if ans == "o":
            approved.append(node)
        elif ans == "a":
            return None
        else:
            print("        ⏭️  нода пропущена")

    skewed = [(n, c[:9]) for n, c in deployed if c and c != local.commit]
    if approved and skewed:
        print(f"\n  ⚠️ Рассинхрон версий: уже развёрнутые ноды на других версиях "
              f"({', '.join(f'{n}@{s}' for n, s in skewed)}),")
        print(f"     новые серверы получат локальную {local.short}. "
              f"Полная синхронизация всех нод — в ветке обновления.")
    return approved


async def _bind_and_report(db: Database, records: list, targets: list, results: list) -> None:
    """Привязать сервисы к успешно задеплоенным нодам (status=standby) + отчёт."""
    if not records:
        print("\n(нет записей в programdata — привязки в dispatcher.service_status не пишем)")
        return
    ok_nodes = [t for t, r in zip(targets, results) if r.ok]
    if not ok_nodes:
        print("\nНи одна нода не задеплоена успешно — привязки не пишем.")
        return
    print("\n── Привязка сервисов к нодам (dispatcher.service_status) ──")
    for rec in records:
        for node in ok_nodes:
            await db.bind_service_node(rec["program_id"], node["id"], status="standby")
        bindings = await db.get_service_bindings(rec["program_id"])
        parts = [f"{b['server_name'] or b['ip_address']}[{b['status']}]" for b in bindings]
        print(f"  {rec['service_name']:24} → {', '.join(parts) if parts else '—'}")


async def _verify_nodes(ssh: SshClient, targets: list, results: list,
                        remote_folder: str, project_dir: str) -> None:
    """Хэш-сверка содержимого на успешно задеплоенных нодах."""
    ok_nodes = [t for t, r in zip(targets, results) if r.ok]
    if not ok_nodes:
        return
    print("\n── Хэш-сверка содержимого (файлы на ноде = локальным) ──")
    for node in ok_nodes:
        total, ok, bad = await verify_mod.verify_node(
            ssh, node["ip_address"], remote_folder, project_dir)
        mark = "✅" if not bad else "❌"
        line = f"  {(node['server_name'] or node['hostname']):16} {mark} совпало {ok}/{total}"
        if bad:
            line += f"  проблемы: {', '.join(bad[:5])}" + (" …" if len(bad) > 5 else "")
        print(line)


async def run(args=None):
    interactive = not (args and getattr(args, "yes", False))
    ui.set_mode(interactive=interactive, assume_yes=bool(args and getattr(args, "yes", False)))
    dry_run = bool(args and getattr(args, "dry_run", False))
    preselect = getattr(args, "nodes", None) if args else None

    project_dir = (args.project if args and args.project else _ask("Папка проекта для деплоя", os.getcwd()))
    project_dir = os.path.abspath(os.path.expanduser(project_dir))
    if not os.path.isdir(os.path.join(project_dir, "systemd")):
        print(f"⚠️  В {project_dir} нет папки systemd/ — продолжаю, но юниты ставить нечего.")

    local = local_version(project_dir)
    print(f"Проект: {project_dir}\nВерсия: {local.short} ({local.branch})"
          f"{'  ⚠️ DIRTY (незакоммичено)' if local.dirty else ''}{'  [DRY-RUN]' if dry_run else ''}")

    db = Database()
    await db.connect()
    ssh = SshClient()
    try:
        action = _ACTION_MAP.get(getattr(args, "action", None)) if args else None
        action = action or _ask("\nДействие: [1] деплой  [2] статус версий  "
                                "[3] создать запись programdata  [q] выход", "1")
        if action == "q":
            return
        if action == "3":
            from core.programdata import create_record_interactive
            await create_record_interactive(db)
            return

        remote_folder, local_svcs, linked_ips, records = await _resolve_remote_folder(db, project_dir)
        if not remote_folder:
            print("🛑 Путь установки не задан — выходим.")
            return
        print(f"Путь установки на серверах: {remote_folder}")

        nodes = await db.get_online_nodes()
        if not nodes:
            print("🛑 Нет online-нод в vocabulary.nodes.")
            return

        if action == "2":
            targets = _select_nodes(nodes, linked_ips, preselect)
            status_mod.print_status(local, await status_mod.check_status(ssh, targets, remote_folder, local))
            return

        # ---- деплой ----
        if not await validate_mod.validate_paths(db, project_dir):
            print("🛑 Деплой отменён на валидации.")
            return

        targets = _select_nodes(nodes, linked_ips, preselect)
        if not targets:
            print("🛑 Ноды не выбраны.")
            return
        service_files = [s.name for s in local_svcs]

        extra_cmds: list[str] = []
        if config.PROVISION:
            for pkg, cmd in provision_mod.detect_post_install(project_dir):
                if ui.confirm(f"В requirements есть '{pkg}' — нужна отдельная установка ('{cmd}'). "
                              f"Выполнить на нодах?"):
                    extra_cmds.append(cmd)

        targets = await _preflight(ssh, targets, remote_folder, local, service_files)
        if targets is None:
            print("🛑 Деплой отменён (preflight).")
            return
        if not targets:
            print("🛑 После предполётной проверки не осталось нод.")
            return

        print(f"\nБудет {'СУХОЙ ПРОГОН на' if dry_run else 'задеплоено на'} {len(targets)} нод(ы):")
        for n in targets:
            print(f"  • {(n['server_name'] or n['hostname'])} ({n['ip_address']})")
        prov = ("venv+pip" + (" + " + ", ".join(extra_cmds) if extra_cmds else "")) if config.PROVISION else "нет"
        print(f"Код → {remote_folder}; юниты → /etc/systemd/system ({len(service_files)} шт.); "
              f"provisioning: {prov}; версия {local.short}")
        if not dry_run and not ui.confirm("Подтвердить деплой?"):
            print("🛑 Отменено.")
            return

        results = await deploy_mod.deploy(
            ssh, Deployer(ssh), targets, project_dir, remote_folder, service_files,
            local, deployed_by=getpass.getuser(),
            deployed_at=datetime.now().isoformat(timespec="seconds"),
            extra_cmds=extra_cmds, dry_run=dry_run,
        )
        deploy_mod.print_deploy_results(results)

        if dry_run:
            print("\nСухой прогон — изменений не внесено (provision/юниты/привязки/VERSION пропущены).")
            return

        await _bind_and_report(db, records, targets, results)
        await _verify_nodes(ssh, targets, results, remote_folder, project_dir)
        status_mod.print_status(local, await status_mod.check_status(ssh, targets, remote_folder, local))

        audit_mod.write({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "user": getpass.getuser(), "project": project_dir,
            "commit": local.commit, "short": local.short, "dirty": local.dirty,
            "remote_folder": remote_folder, "extra_cmds": extra_cmds,
            "nodes": [{"node": r.node, "ip": r.ip, "ok": r.ok, "step": r.step} for r in results],
        })
    finally:
        await ssh.close_all()
        await db.close()
