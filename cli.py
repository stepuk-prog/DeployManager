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

# Три основные ветки + служебные действия для автоматизации.
_ACTION_MAP = {"new": "1", "add": "2", "check": "3", "dashboard": "3", "status": "3",
               "create": "create", "state": "state", "manage": "manage", "uninstall": "uninstall",
               "sync": "sync", "env": "sync"}


async def _ask(prompt: str, default: str = "") -> str:
    return await ui.ask(prompt, default)


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


async def _select_nodes(nodes: list, linked_ips: set[str], preselect: str | None = None) -> list:
    if preselect:
        return _parse_selection(nodes, preselect)
    labels = [f"{'*' if n['ip_address'] in linked_ips else ' '} {(n['server_name'] or n['hostname']):18} "
              f"{n['ip_address']:16} {n['description'] or ''}" for n in nodes]
    idxs = await ui.checkbox("Ноды для операции (* — связаны с программой; пробел — отметить, enter — ок):", labels)
    return [nodes[i] for i in idxs]


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
        return (await _ask("Не нашёл программу в programdata. Путь установки вручную", "") or None), \
            local_svcs, linked_ips, records
    print(f"⚠️  В programdata разные пути: {folders}")
    return (await _ask("Укажи путь установки вручную", sorted(folders)[0]) or None), \
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
        ans = (await ui.ask("        [o] перезаписать / [s] пропустить ноду / [a] отмена всего", "s")).lower()
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


async def _report_units(ssh: SshClient, targets: list, results: list,
                        service_files: list[str]) -> None:
    """Per-node подтверждение: service-файлы реально лежат в /etc/systemd/system."""
    ok_nodes = [t for t, r in zip(targets, results) if r.ok]
    if not ok_nodes or not service_files:
        return
    print("\n── Установка юнитов (/etc/systemd/system) ──")
    for node in ok_nodes:
        ip = node["ip_address"]
        name = node["server_name"] or node["hostname"]
        present, missing = [], []
        for sf in service_files:
            (present if await ssh.path_exists(ip, f"{config.SYSTEMD_DIR}/{sf}") else missing).append(sf)
        if not missing:
            print(f"  {name:16} ✅ скопирован: {', '.join(present)}")
        else:
            ok_part = f"скопирован: {', '.join(present)}; " if present else ""
            print(f"  {name:16} ❌ {ok_part}НЕ скопирован: {', '.join(missing)}")


async def _bind_and_report(db: Database, records: list, targets: list, results: list) -> None:
    """Привязать сервисы к успешно задеплоенным нодам (status=standby) + per-node отчёт
    (добавлена / уже была / ошибка)."""
    if not records:
        print("\n(нет записей в programdata — привязки в dispatcher.service_status не пишем)")
        return
    ok_nodes = [t for t, r in zip(targets, results) if r.ok]
    if not ok_nodes:
        print("\nНи одна нода не задеплоена успешно — привязки не пишем.")
        return
    print("\n── Привязка сервисов к нодам (dispatcher.service_status) ──")
    for rec in records:
        print(f"  {rec['service_name']}")
        for node in ok_nodes:
            name = node["server_name"] or node["hostname"]
            try:
                res = await db.bind_service_node(rec["program_id"], node["id"], status="standby")
            except Exception as e:                      # noqa: BLE001 — отчитаться, не падать
                print(f"    {name:16} ❌ не записалась: {e}")
                continue
            if res == "inserted":
                print(f"    {name:16} ✅ добавлена [standby]")
            else:                                       # 'kept:<status>' — строка уже была
                print(f"    {name:16} • уже была [{res.split(':', 1)[1]}]")


def _node_flags(step: str) -> tuple[bool, bool]:
    """Из шага DeployResult → (folder_deployed, service_installed)."""
    folder = step not in ("ping", "rsync")
    service = step in ("write_version", "done")
    return folder, service


async def _journal_deploy(db: Database, records: list, targets: list, results: list,
                          local, action: str) -> None:
    """Записать журнал по каждой (программа × нода): флаги шагов + результат."""
    operator = getpass.getuser()
    for rec in records:
        for node, res in zip(targets, results):
            folder, service = _node_flags(res.step)
            await db.journal_write(
                rec["program_id"], node["id"], action,
                folder_deployed=folder, service_installed=service, db_updated=res.ok,
                result=("ok" if res.ok else res.step), commit=local.commit, operator=operator,
                details={"ip": node["ip_address"], "detail": res.detail})


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


async def _leader_guard(db: Database, records: list, targets: list) -> list:
    """Предупредить, если среди целей есть активные leader-ноды (деплой перезапишет
    работающий код). Возвращает targets (возможно без leader-нод, если оператор отказался)."""
    leader_of: dict[int, list[str]] = {}
    for rec in records:
        for b in await db.get_service_bindings(rec["program_id"]):
            if b["status"] == "leader":
                leader_of.setdefault(b["node_id"], []).append(rec["service_name"])
    hit = [(n, leader_of[n["id"]]) for n in targets if n["id"] in leader_of]
    if not hit:
        return targets
    print("\n  ⚠️⚠️ ВНИМАНИЕ: среди целей есть АКТИВНЫЕ leader-ноды:")
    for n, svcs in hit:
        print(f"      {(n['server_name'] or n['ip_address'])} — leader для: {', '.join(svcs)}")
    print("      Деплой перезапишет РАБОТАЮЩИЙ код. Рекомендуется сначала переключить")
    print("      программу на другой сервер через диспетчер, затем деплоить.")
    if await ui.confirm("Всё равно деплоить на активные leader-ноды?"):
        return targets
    leader_ids = {n["id"] for n, _ in hit}
    filtered = [n for n in targets if n["id"] not in leader_ids]
    print(f"      ⏭️  Leader-ноды исключены ({len(leader_ids)}); останется {len(filtered)}.")
    return filtered


async def _show_deployment_map(db: Database, records: list) -> None:
    """Где проект уже развёрнут (для режима «добавить сервер»)."""
    print("\n── Текущее развёртывание (где уже стоит) ──")
    any_bound = False
    for rec in records:
        bindings = await db.get_service_bindings(rec["program_id"])
        any_bound = any_bound or bool(bindings)
        parts = [f"{b['server_name'] or b['ip_address']}[{b['status']}]" for b in bindings]
        print(f"  {rec['service_name']:24} → {', '.join(parts) if parts else '— нигде'}")
    if not any_bound:
        print("  (привязок нет — проект ещё нигде не развёрнут; это скорее режим [1] «с нуля»)")


async def _deploy_flow(db: Database, ssh: SshClient, project_dir: str, local,
                       remote_folder: str, local_svcs: list, records: list, nodes: list,
                       linked_ips: set, preselect: str | None, dry_run: bool, add_server: bool) -> None:
    """Общий pipeline деплоя для веток «с нуля» и «добавить сервер»."""
    if add_server:
        await _show_deployment_map(db, records)

    if not await validate_mod.validate_paths(db, project_dir):
        print("🛑 Деплой отменён на валидации.")
        return

    targets = await _select_nodes(nodes, linked_ips, preselect)
    if not targets:
        print("🛑 Ноды не выбраны.")
        return
    service_files = [s.name for s in local_svcs]

    extra_cmds: list[str] = []
    if config.PROVISION:
        for pkg, cmd in provision_mod.detect_post_install(project_dir):
            if await ui.confirm(f"В requirements есть '{pkg}' — нужна отдельная установка ('{cmd}'). "
                                f"Выполнить на нодах?"):
                extra_cmds.append(cmd)

    targets = await _preflight(ssh, targets, remote_folder, local, service_files)
    if targets is None:
        print("🛑 Деплой отменён (preflight).")
        return
    if not targets:
        print("🛑 После предполётной проверки не осталось нод.")
        return

    targets = await _leader_guard(db, records, targets)
    if not targets:
        print("🛑 После исключения активных leader-нод не осталось целей.")
        return

    print(f"\nБудет {'СУХОЙ ПРОГОН на' if dry_run else 'задеплоено на'} {len(targets)} нод(ы):")
    for n in targets:
        print(f"  • {(n['server_name'] or n['hostname'])} ({n['ip_address']})")
    prov = ("venv+pip" + (" + " + ", ".join(extra_cmds) if extra_cmds else "")) if config.PROVISION else "нет"
    print(f"Код → {remote_folder}; юниты → /etc/systemd/system ({len(service_files)} шт.); "
          f"provisioning: {prov}; версия {local.short}")
    if not dry_run and not await ui.confirm("Подтвердить деплой?"):
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

    await _report_units(ssh, targets, results, service_files)
    await _bind_and_report(db, records, targets, results)
    await _verify_nodes(ssh, targets, results, remote_folder, project_dir)
    status_mod.print_status(local, await status_mod.check_status(ssh, targets, remote_folder, local))
    await _journal_deploy(db, records, targets, results, local, "add_server" if add_server else "deploy")

    audit_mod.write({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user": getpass.getuser(), "project": project_dir, "mode": "add" if add_server else "new",
        "commit": local.commit, "short": local.short, "dirty": local.dirty,
        "remote_folder": remote_folder, "extra_cmds": extra_cmds,
        "nodes": [{"node": r.node, "ip": r.ip, "ok": r.ok, "step": r.step} for r in results],
    })


async def run(args=None):
    interactive = not (args and getattr(args, "yes", False))
    ui.set_mode(interactive=interactive, assume_yes=bool(args and getattr(args, "yes", False)))
    dry_run = bool(args and getattr(args, "dry_run", False))
    preselect = getattr(args, "nodes", None) if args else None

    project_dir = (args.project if args and args.project else await _ask("Папка проекта", os.getcwd()))
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
        action = action or await _ask(
            "\nРежим:\n"
            "  [1] деплой нового проекта (с нуля на чистые серверы)\n"
            "  [2] добавить сервер к существующему деплою\n"
            "  [3] проверить версии на серверах (vs локальной)\n"
            "  [4] обновить .env / service-файлы на серверах (без передеплоя)\n"
            "  [q] выход\nВыбор", "1")
        if action == "4":
            action = "sync"
        if action == "q":
            return
        if action == "create":   # служебное (для автоматизации)
            from core.programdata import create_record_interactive
            await create_record_interactive(db)
            return
        if action == "state":     # служебное: обновить running в service_status
            from core import state
            await state.check_state(ssh, db, project_dir)
            return
        if action == "manage":     # служебное: start/stop/restart через watchdog диспетчера
            from core import watchdog
            await watchdog.manage(db, project_dir, command=getattr(args, "command", None),
                                  preselect=preselect)
            return
        if action == "uninstall":  # деструктивное: снять с ноды (только свои service_name)
            from core import uninstall
            await uninstall.uninstall(ssh, db, project_dir, preselect=preselect)
            return

        remote_folder, local_svcs, linked_ips, records = await _resolve_remote_folder(db, project_dir)
        if not remote_folder:
            print("🛑 Путь установки не задан — выходим.")
            return
        print(f"Путь установки на серверах: {remote_folder}")

        if action == "sync":   # обновить .env / service-файлы на нодах без передеплоя
            from core import sync_config
            await sync_config.sync_config(ssh, db, project_dir, remote_folder, local_svcs,
                                          records, dry_run=dry_run)
            return

        if action == "3":   # ── проверить версии (state-check + дашборд + опц. управление) ──
            from core import dashboard, state
            await state.check_state(ssh, db, project_dir)   # сперва опрос нод → свежий running в БД
            await dashboard.show(ssh, db, project_dir, local)  # дашборд уже с фактическим состоянием
            if await ui.confirm("Управление сервисом (start/stop/restart через диспетчер)?"):
                from core import watchdog
                await watchdog.manage(db, project_dir)
            return

        nodes = await db.get_online_nodes()
        if not nodes:
            print("🛑 Нет online-нод в vocabulary.nodes.")
            return

        # ── ветки 1 (с нуля) и 2 (добавить сервер) — общий pipeline ──
        await _deploy_flow(db, ssh, project_dir, local, remote_folder, local_svcs, records,
                           nodes, linked_ips, preselect, dry_run, add_server=(action == "2"))
    finally:
        await ssh.close_all()
        await db.close()
