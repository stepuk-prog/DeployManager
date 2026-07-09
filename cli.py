"""CLI DeployManager (интерактивный + неинтерактивный через args).

Поток деплоя: папка проекта → git-версия → валидация (БД↔файлы) → предложение
доустановок → preflight (защита/добавление серверов) → rsync(.env+код) → provision →
юниты → VERSION → привязка standby + отчёт → хэш-сверка → статус → audit-лог.
"""
import getpass
import os
from datetime import datetime

from classes import Deployer, SshClient
from classes.manifest import build_manifest, local_version, parse_manifest
from core import audit as audit_mod
from core import deploy as deploy_mod
from core import provision as provision_mod
from core import status as status_mod
from core import ui
from core import validate as validate_mod
from core import verify as verify_mod
from database import Database
from settings import config
import tools

# Три основные ветки + служебные действия для автоматизации.
_ACTION_MAP = {"new": "1", "add": "2", "check": "3", "dashboard": "3", "status": "3",
               "create": "create", "state": "state", "manage": "manage", "uninstall": "uninstall",
               "sync": "sync", "env": "sync", "infra": "infra", "sessions": "sessions",
               "cookies": "cookies", "setup-node": "setup-node", "node": "setup-node",
               "reporter": "reporter"}


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
    # ноды, где программа уже стоит (связаны), предотмечаем — обычно операция именно по ним.
    default_checked = [n["ip_address"] in linked_ips for n in nodes]
    idxs = await ui.checkbox("Ноды для операции (* — связаны с программой; пробел — отметить, enter — ок):",
                             labels, default_checked=default_checked)
    return [nodes[i] for i in idxs]


async def select_services(local_svcs: list, records: list) -> tuple[list, list]:
    """Выбрать, с какими service-файлами (= программами) работаем — каждый файл это отдельная
    запись programdata. Шаблонные юниты (@) — НЕ программы (нет записи в programdata, инстансы
    этим инструментом не привязываются/не запускаются), поэтому в выбор и установку не попадают
    (их исходник всё равно приедет на ноду вместе с папкой через rsync).

    Юниты подаются ПОРЦИЯМИ по каталогам systemd/ (OTC/Binary/Crypto…): если каталогов >1 —
    сперва выбор каталогов, затем уточнение юнитов внутри выбранных (все предотмечены, лишние
    можно снять). Один каталог (плоский systemd/) — шаг каталогов пропускается.
    Возвращает (выбранные не-шаблонные local_svcs, выбранные records). При ≤1 программе — без вопроса."""
    progs = [s for s in local_svcs if not s.is_template]
    if len(progs) <= 1:
        return progs, records
    rec_by_name = {r["service_name"]: r for r in records}

    # ── шаг 1: каталоги-порции (если их больше одного) ──
    groups: dict[str, list] = {}
    for s in progs:
        groups.setdefault(s.group, []).append(s)
    if len(groups) > 1:
        gnames = sorted(groups, key=lambda g: (g == "", g))   # корень ('') — в конец
        glabels = [f"{(g or '(корень systemd/)'):20} {len(groups[g]):>3} шт." for g in gnames]
        gidxs = await ui.checkbox(
            "Каталоги-порции systemd/ для операции (пробел — отметить, enter — ок):",
            glabels, default_all=True)
        if not gidxs:
            return [], []
        progs = [s for i in gidxs for s in groups[gnames[i]]]

    # ── шаг 2: уточнение юнитов внутри выбранных каталогов ──
    labels = []
    for s in progs:
        r = rec_by_name.get(s.name)
        tag = (r["program_name"] or "—") if r else "‼️ нет записи в programdata"
        gpref = f"{s.group}/" if s.group else ""
        labels.append(f"{gpref}{s.name:26} {tag}")
    idxs = await ui.checkbox(
        "Service-файлы (= программы) для операции "
        "(каждый — отдельная запись programdata; пробел — отметить/снять, enter — ок):",
        labels, default_all=True)
    if not idxs:
        return [], []
    chosen = {progs[i].name for i in idxs}
    sel_svcs = [s for s in progs if s.name in chosen]
    sel_recs = [r for r in records if r["service_name"] in chosen]
    return sel_svcs, sel_recs


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
        # Записей в programdata ещё нет — путь установки берём из WorkingDirectory юнитов
        # (единственный источник истины для нового деплоя), а не спрашиваем вслепую.
        wd = {s.working_dir.rstrip("/") for s in local_svcs
              if not s.is_template and s.working_dir and s.working_dir.startswith("/")}
        if len(wd) == 1:
            return wd.pop(), local_svcs, linked_ips, records
        if not wd:
            print("🛑 В service-файлах нет абсолютного WorkingDirectory — юниты нерабочие, "
                  "деплоить некуда. Пропишите WorkingDirectory= (абсолютный путь) в юнитах.")
        else:
            print(f"🛑 В юнитах разные WorkingDirectory: {sorted(wd)}. Набор юнитов одного проекта "
                  f"ставится в ОДНУ папку — приведите WorkingDirectory к единому пути.")
        return None, local_svcs, linked_ips, records
    print(f"⚠️  В programdata разные пути: {folders}")
    return (await _ask("Укажи путь установки вручную", sorted(folders)[0]) or None), \
        local_svcs, linked_ips, records


async def _preflight(ssh: SshClient, db: Database, targets: list, remote_folder: str,
                     local, service_files: list[str], records: list,
                     project_dir: str) -> tuple[list, list] | None:
    """Предполётная проверка (первичный деплой / добавление серверов).

      • чисто (нет папки/юнитов) → новый сервер, полный деплой;
      • есть наш VERSION + КОД совпадает (хэш-сверка, без systemd/*.service и requirements.txt):
          — не хватает юнитов/связей или изменился requirements.txt → ЛЁГКИЙ путь (доставить юниты,
            при изменении requirements — pip install -r, связи в БД; без полного передеплоя);
          — всё на месте → пропускаем;
      • есть VERSION, но КОД расходится → синхронизация в ветке обновления, пропускаем;
      • папка/юнит без VERSION → чужое/частичное → спросить (перезаписать/пропустить/отмена).
    Возвращает (approved_full, light_targets) либо None — полная отмена.
    light_target: {node, units:[недостающие .service], records:[записи], req_changed:bool}.
    """
    print("\n── Предполётная проверка нод ──")
    version_path = f"{remote_folder.rstrip('/')}/{config.VERSION_FILE}"
    approved: list = []
    light: list[dict] = []
    deployed: list[tuple[str, str]] = []
    # к каким нодам уже привязаны выбранные записи (для определения «не хватает связи»)
    bound: dict[int, set[int]] = {}
    for r in records:
        bound[r["program_id"]] = {b["node_id"] for b in await db.get_service_bindings(r["program_id"])}
    for node in targets:
        ip = node["ip_address"]
        name = node["server_name"] or node["hostname"]
        if not await ssh.ping(ip):
            print(f"  {name:16} 🔌 недоступна — пропускаю")
            continue
        man = parse_manifest(await ssh.read_file(ip, version_path))
        if man is not None:
            short = man.get("short") or (man.get("commit", "")[:9])
            # сверяем КОД без service-файлов И requirements.txt: их обрабатываем отдельно
            # (юниты доустанавливаем; requirements сверяем ниже отдельно → возможно нужны новые
            # библиотеки). Иначе «добавить новый юнит» / «появилась новая зависимость» не сработали бы.
            total, ok, bad = await verify_mod.verify_node(
                ssh, ip, remote_folder, project_dir, ignore_globs=["systemd/*.service", "requirements.txt"])
            if bad:
                print(f"  {name:16} ↩️  развёрнут v{short}, но КОД расходится "
                      f"({len(bad)}/{total}) — синхронизация в ветке обновления, пропускаю")
                deployed.append((name, man.get("commit", "")))
                continue
            # отдельная сверка requirements.txt: если изменился — на ноде могут понадобиться новые
            # библиотеки (pip install -r при доустановке).
            lf = verify_mod.local_hashes(project_dir, ["requirements.txt"])
            rf = await verify_mod.remote_hashes(ssh, ip, remote_folder, ["requirements.txt"])
            req_changed = lf.get("requirements.txt") != rf.get("requirements.txt")
            missing_units = [sf for sf in service_files
                             if not await ssh.path_exists(ip, f"{config.SYSTEMD_DIR}/{sf}")]
            unbound = [r for r in records if node["id"] not in bound.get(r["program_id"], set())]
            if not missing_units and not unbound and not req_changed:
                print(f"  {name:16} ✅ уже полностью развёрнут (код+юниты+связи+зависимости совпадают)")
                deployed.append((name, man.get("commit", "")))
                continue
            print(f"  {name:16} ✓ код сверен (sha256 {ok}/{total} совпало, v{short}), не хватает:")
            if missing_units:
                print(f"        юниты: {', '.join(missing_units)}")
            if unbound:
                print(f"        связи в БД: {', '.join(r['service_name'] for r in unbound)}")
            if req_changed:
                print(f"        ⚠️ requirements.txt ОТЛИЧАЕТСЯ → нужны новые библиотеки (pip install -r)")
            idx = await ui.select(
                f"{name}: код совпадает, не хватает юнитов/связей"
                f"{'/зависимостей' if req_changed else ''}. Действие?",
                ["✅ Доставить + настроить", "⏭️ Пропустить", "🛑 Отмена всего"], default_index=0)
            if idx == 0:
                light.append({"node": node, "units": missing_units, "records": records,
                              "req_changed": req_changed})
            elif idx == 2:
                return None
            else:
                print("        ⏭️  нода пропущена")
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
        idx = await ui.select(
            f"{name}: уже есть папка/юниты (не наш деплой). Что делать?",
            ["⚠️ Перезаписать", "⏭️ Пропустить ноду", "🛑 Отмена всего"], default_index=1)
        if idx == 0:
            approved.append(node)
        elif idx == 2:
            return None
        else:
            print("        ⏭️  нода пропущена")

    skewed = [(n, c[:9]) for n, c in deployed if c and c != local.commit]
    if approved and skewed:
        print(f"\n  ⚠️ Рассинхрон версий: уже развёрнутые ноды на других версиях "
              f"({', '.join(f'{n}@{s}' for n, s in skewed)}),")
        print(f"     новые серверы получат локальную {local.short}. "
              f"Полная синхронизация всех нод — в ветке обновления.")
    return approved, light


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


async def _journal_deploy(db: Database, records: list, targets: list, results: list,
                          local, action: str) -> None:
    """Записать журнал по каждой (программа × нода): флаги шагов + результат."""
    operator = getpass.getuser()
    for rec in records:
        for node, res in zip(targets, results):
            folder, service = deploy_mod.node_flags(res.step)
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


async def _install_units_light(db: Database, ssh: SshClient, project_dir: str, remote_folder: str,
                               light_targets: list[dict], local, dry_run: bool) -> bool:
    """Лёгкий путь: код на ноде уже совпадает — НЕ передеплоиваем целиком, а доставляем service-файлы
    (rsync systemd/ → install в /etc) и настраиваем связи в БД. sync_units доставит и НОВЫЕ юниты.
    Если requirements.txt изменился (req_changed) — доставляем код-папку (rsync) и ставим зависимости
    (provision: venv + pip install -r, БЕЗ playwright). VERSION обновляем. → True, если что-то делалось."""
    if not light_targets:
        return False
    print(f"\n── Доустановка юнитов (код совпадает: service-файлы → /etc, при изменении requirements "
          f"— pip install, + связи в БД){' [DRY-RUN]' if dry_run else ''} ──")
    deployer = Deployer(ssh)
    operator = getpass.getuser()
    manifest_json = build_manifest(local, operator, datetime.now().isoformat(timespec="seconds"))
    for lt in light_targets:
        node, units, recs = lt["node"], lt["units"], lt["records"]
        req_changed = lt.get("req_changed", False)
        ip = node["ip_address"]
        name = node["server_name"] or node["hostname"]
        if dry_run:
            print(f"  {name:16} юниты: {', '.join(units) or '—'}; "
                  f"requirements: {'ИЗМЕНИЛСЯ → pip install -r' if req_changed else 'без изменений'}; "
                  f"связи: {', '.join(r['service_name'] for r in recs)}")
            continue
        ok = True
        if req_changed:
            # requirements изменился → доставляем папку (rsync подтянет новый requirements + юниты,
            # совпадающий код — no-op) и ставим зависимости (без playwright), затем юниты в /etc.
            ok = await deployer.rsync_project(ip, project_dir, remote_folder)
            if ok and config.PROVISION:
                ok = await deployer.provision(ip, remote_folder, [])
            elif ok and not config.PROVISION:
                print(f"  {name:16} ⚠️ requirements изменился, но PROVISION выключен — зависимости НЕ ставлю")
            if ok and units:
                ok = await deployer.install_services(ip, remote_folder, units)
            print(f"  {name:16} {'✅' if ok else '⛔'} requirements обновлён + зависимости установлены"
                  + (f"; юниты: {', '.join(units)}" if units else ""))
            if ok and not await deployer.write_version(ip, remote_folder, manifest_json):
                print(f"  {name:16} ⚠️ VERSION не записан (нода покажется stale)")
                ok = False
        elif units:
            ok = await deployer.sync_units(ip, project_dir, remote_folder, units)
            print(f"  {name:16} {'✅' if ok else '⛔'} юниты доставлены+установлены: {', '.join(units)}")
            if ok and not await deployer.write_version(ip, remote_folder, manifest_json):
                print(f"  {name:16} ⚠️ VERSION не записан (нода покажется stale)")
                ok = False
        else:
            print(f"  {name:16} • юниты уже на месте")
        for r in recs:
            try:
                res = await db.bind_service_node(r["program_id"], node["id"], status="standby")
            except Exception as e:                      # noqa: BLE001 — отчитаться, не падать
                print(f"      {r['service_name']:24} ❌ привязка не записалась: {e}")
                continue
            mark = "✅ привязана [standby]" if res == "inserted" else f"• уже была [{res.split(':', 1)[1]}]"
            print(f"      {r['service_name']:24} {mark}")
        for r in recs:
            await db.journal_write(
                r["program_id"], node["id"], "attach_unit",
                folder_deployed=True, service_installed=ok, db_updated=True,
                result=("ok" if ok else "light_install"), commit=local.commit,
                operator=operator,
                details={"ip": ip, "units": units, "req_changed": req_changed, "light": True})
    return True


async def _deploy_flow(db: Database, ssh: SshClient, project_dir: str, local,
                       remote_folder: str, local_svcs: list, records: list, nodes: list,
                       linked_ips: set, preselect: str | None, dry_run: bool, add_server: bool) -> None:
    """Общий pipeline деплоя для веток «с нуля» и «добавить сервер»."""
    # Каждый service-файл — отдельная программа (своя запись programdata): уточняем, с какими работаем.
    local_svcs, records = await select_services(local_svcs, records)
    if not local_svcs:
        print("🛑 Не выбран ни один service-файл.")
        return

    if add_server:
        await _show_deployment_map(db, records)

    only = {s.name for s in local_svcs if not s.is_template}
    if not await validate_mod.validate_paths(db, project_dir, only=only):
        print("🛑 Деплой отменён на валидации.")
        return
    # перечитываем записи: в валидации (_resolve_missing) могли создать новые — их тоже привязать.
    records = await db.find_programs_by_service(sorted(only))

    targets = await _select_nodes(nodes, linked_ips, preselect)
    if not targets:
        print("🛑 Ноды не выбраны.")
        return
    service_files = [s.name for s in local_svcs]

    pf = await _preflight(ssh, db, targets, remote_folder, local, service_files, records, project_dir)
    if pf is None:
        print("🛑 Деплой отменён (preflight).")
        return
    targets, light_targets = pf
    if not targets and not light_targets:
        print("🛑 После предполётной проверки не осталось нод.")
        return

    if targets:
        targets = await _leader_guard(db, records, targets)
        if not targets and not light_targets:
            print("🛑 После исключения активных leader-нод не осталось целей.")
            return

    # provision/post-install (playwright и т.п.) спрашиваем ТОЛЬКО при полном деплое — для лёгкой
    # доустановки юнитов код уже на ноде, rsync/provision не выполняется (нечего ставить).
    extra_cmds: list[str] = []
    if targets and config.PROVISION:
        for pkg, cmd in provision_mod.detect_post_install(project_dir):
            if await ui.confirm(f"В requirements есть '{pkg}' — нужна отдельная установка ('{cmd}'). "
                                f"Выполнить на нодах?"):
                extra_cmds.append(cmd)

    # ── план операции ──
    if targets:
        print(f"\nБудет {'СУХОЙ ПРОГОН на' if dry_run else 'задеплоено (полностью) на'} {len(targets)} нод(ы):")
        for n in targets:
            print(f"  • {(n['server_name'] or n['hostname'])} ({n['ip_address']})")
        prov = ("venv+pip" + (" + " + ", ".join(extra_cmds) if extra_cmds else "")) if config.PROVISION else "нет"
        print(f"Код → {remote_folder}; юниты → /etc/systemd/system ({len(service_files)} шт.); "
              f"provisioning: {prov}; версия {local.short}")
    if light_targets:
        print(f"\nДоустановка юнитов (код уже совпадает) на {len(light_targets)} нод(ы) — "
              f"rsync service-файлов + установка в /etc + связи в БД, без передеплоя/provision.")

    if not dry_run and not await ui.confirm("Подтвердить операцию?"):
        print("🛑 Отменено.")
        return

    results: list = []
    if targets:
        results = await deploy_mod.deploy(
            ssh, Deployer(ssh), targets, project_dir, remote_folder, service_files,
            local, deployed_by=getpass.getuser(),
            deployed_at=datetime.now().isoformat(timespec="seconds"),
            extra_cmds=extra_cmds, dry_run=dry_run,
        )
        deploy_mod.print_deploy_results(results)

    if dry_run:
        await _install_units_light(db, ssh, project_dir, remote_folder, light_targets, local, dry_run=True)
        print("\nСухой прогон — изменений не внесено (provision/юниты/привязки/VERSION пропущены).")
        return

    if targets:
        await _report_units(ssh, targets, results, service_files)
        await _bind_and_report(db, records, targets, results)
        await _verify_nodes(ssh, targets, results, remote_folder, project_dir)
        status_mod.print_status(
            local, await status_mod.check_status(ssh, targets, remote_folder, local, project_dir))
        await _journal_deploy(db, records, targets, results, local, "add_server" if add_server else "deploy")

    await _install_units_light(db, ssh, project_dir, remote_folder, light_targets, local, dry_run=False)

    audit_mod.write({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user": getpass.getuser(), "project": project_dir, "mode": "add" if add_server else "new",
        "commit": local.commit, "short": local.short, "dirty": local.dirty,
        "remote_folder": remote_folder, "extra_cmds": extra_cmds,
        "nodes": [{"node": r.node, "ip": r.ip, "ok": r.ok, "step": r.step} for r in results],
        "light_nodes": [{"node": lt["node"]["server_name"] or lt["node"]["hostname"],
                         "ip": lt["node"]["ip_address"], "units": lt["units"]} for lt in light_targets],
    })


async def run(args=None):
    interactive = not (args and getattr(args, "yes", False))
    ui.set_mode(interactive=interactive, assume_yes=bool(args and getattr(args, "yes", False)))
    dry_run = bool(args and getattr(args, "dry_run", False))
    preselect = getattr(args, "nodes", None) if args else None

    db = Database()
    await db.connect()
    ssh = SshClient()
    try:
        from core import scripts as scripts_mod
        raw_action = getattr(args, "action", None) if args else None
        # script-ключи не в _ACTION_MAP — сохраняем их как есть (иначе уйдём в промпт)
        action = _ACTION_MAP.get(raw_action) or (raw_action if raw_action in scripts_mod.SCRIPT_KEYS else None)
        action = action or await _ask(
            "\nРежим:\n"
            "  [1] деплой нового проекта (с нуля на чистые серверы)\n"
            "  [2] добавить сервер к существующему деплою\n"
            "  [3] проверить версии на серверах (vs локальной)\n"
            "  [4] обновить .env / service-файлы на серверах (без передеплоя)\n"
            "  [5] деплой инфра-компонента диспетчера (GD / WD / CD / DispatcherCtl)\n"
            "  [6] юзерботы (сессии): логин → session_string в telegram.telegram\n"
            "  [7] cookies (OTC/Screen/TV/Binodex) — GUI-only, видимый браузер\n"
            "  [8] настроить новую ноду (bootstrap → тип → регистрация → Watchdog)\n"
            "  [q] выход\nВыбор", "1")
        if action == "4":
            action = "sync"
        if action == "5":
            action = "infra"
        if action == "6":
            action = "sessions"
        if action == "7":
            action = "cookies"
        if action == "8":
            action = "setup-node"
        if action == "q":
            return
        if action == "setup-node":   # turnkey ввод новой ноды — SSH+БД, папка проекта не нужна
            from core import setup_node
            await setup_node.run_setup_node(db, ssh, dry_run=dry_run)
            return
        if action == "reporter":   # Reporter на cluster-ноды (Patroni-callback) — БД+SSH, проект не нужен
            from core import reporter
            await reporter.run_reporter(db, ssh, dry_run=dry_run)
            return
        if action == "infra":   # control-plane (GD/WD/CD/DispatcherCtl) — в обход programdata
            from core import infra_deploy
            # операция из флагов; иначе None → infra-флоу спросит меню'ю (как в GUI по кнопке)
            op = "check" if getattr(args, "check", False) else ("dry-run" if dry_run else None)
            await infra_deploy.run_infra(db, ssh, component=getattr(args, "component", None),
                                         operation=op)
            return
        if action in tools.TOOL_KEYS:   # суб-инструменты (сессии и будущие) — БД есть, проект/SSH не нужны
            tool = tools.get_tool(action)
            if tool["kind"] == "screen":   # GUI-only экран (свой UI/браузер) — из CLI не запускается
                print(f"🍪 «{tool['label']}» — GUI-only инструмент (видимый браузер). "
                      f"Запусти через GUI: .venv/bin/python gui_main.py")
                return
            await tools.run_tool(action, db)
            return
        if action in scripts_mod.SCRIPT_KEYS:   # операционные скрипты флота (БД+SSH, проект не нужен)
            await scripts_mod.run_script(action, db, ssh, dry_run=dry_run)
            return

        # ── все прочие ветки требуют папку проекта ──
        project_dir = (args.project if args and args.project
                       else await _ask("Папка проекта", os.getcwd()))
        project_dir = os.path.abspath(os.path.expanduser(project_dir))
        if not os.path.isdir(os.path.join(project_dir, "systemd")):
            print(f"⚠️  В {project_dir} нет папки systemd/ — продолжаю, но юниты ставить нечего.")
        local = local_version(project_dir)
        print(f"Проект: {project_dir}\nВерсия: {local.short} ({local.branch})"
              f"{'  ⚠️ DIRTY (незакоммичено)' if local.dirty else ''}"
              f"{'  [DRY-RUN]' if dry_run else ''}")
        if action == "create":   # служебное: создать записи programdata для юнитов проекта
            from core.programdata import create_record_interactive
            local = validate_mod.list_local_services(project_dir)
            names = [s.name for s in local if not s.is_template]
            existing = {r["service_name"] for r in await db.find_programs_by_service(names)}
            todo = [s for s in local if not s.is_template and s.name not in existing]
            if not todo:
                print("Все юниты проекта уже есть в programdata.")
            for s in todo:
                await create_record_interactive(db, project_dir, s.name, s.working_dir)
            return
        if action == "state":     # служебное: обновить running в service_status
            from core import state
            await state.check_state(ssh, db, project_dir)
            return
        if action == "manage":     # служебное: start/stop/restart через GD (§13 control_request)
            from core import watchdog
            await watchdog.manage(db, project_dir, command=getattr(args, "command", None))
            return
        if action == "uninstall":  # деструктивное: снять с ноды (только свои service_name)
            from core import uninstall
            await uninstall.uninstall(ssh, db, project_dir, preselect=preselect)
            return

        remote_folder, local_svcs, linked_ips, records = await _resolve_remote_folder(db, project_dir)
        if not remote_folder:
            print("🛑 Путь установки не задан — выходим.")
            return
        if not remote_folder.startswith("/"):
            print(f"🛑 Путь установки НЕ абсолютный: {remote_folder!r} — должен начинаться с '/'.\n"
                  f"   Из-за относительного пути rsync/cp бьют мимо (от домашней папки). Исправьте "
                  f"WorkingDirectory/ExecStart в service-файле и folder в programdata.")
            return
        print(f"Путь установки на серверах: {remote_folder}")

        if action == "sync":   # обновить .env / service-файлы на нодах без передеплоя
            from core import sync_config
            await sync_config.sync_config(ssh, db, project_dir, remote_folder, local_svcs,
                                          records, dry_run=dry_run)
            return

        if action == "3":   # ── проверить версии (state-check + дашборд + опц. синхронизация/управление) ──
            from core import cleanup, dashboard, state
            await state.check_state(ssh, db, project_dir)   # сперва опрос нод → свежий running в БД
            stale = await dashboard.show(ssh, db, project_dir, local)  # дашборд + список отставших нод
            nodes = await db.get_online_nodes()
            if stale:
                names = ", ".join(f"{s['name']}({s['lag']})" for s in stale)
                if await ui.confirm(
                        f"Обнаружен рассинхрон версий на {len(stale)} нод(ах): {names}.\n"
                        f"Синхронизировать их до локальной {local.short}?", danger=True):
                    from core import update
                    await update.update(ssh, db, project_dir, remote_folder, local_svcs,
                                        records, local, nodes, stale, dry_run=dry_run)
            # перед проверкой лишних файлов — опц. очистка логов (*.log → truncate -s 0)
            await cleanup.clear_logs(ssh, remote_folder, nodes, linked_ips, dry_run=dry_run)
            # после сведения версий — пост-проверка: лишние файлы на нодах + requirements.txt
            await cleanup.post_check(ssh, project_dir, remote_folder, nodes, linked_ips, local,
                                     dry_run=dry_run)
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
