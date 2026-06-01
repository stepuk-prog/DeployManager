"""Обновление конфигурации на уже развёрнутых нодах БЕЗ полного передеплоя.

Что умеет (по выбору):
  • .env          — залить новый .env (поменялись настройки);
  • service-файлы — обновить юниты в /etc/systemd/system + systemctl daemon-reload.
Цель — ноды, где проект привязан (dispatcher.service_status). Это НЕ обновление кода/версии
(оно — отдельная «ветка обновления»); здесь только конфиг + юниты. После — опц. restart через
watchdog, чтобы изменения применились.
"""
import getpass
import os
from datetime import datetime

from classes.deployer import Deployer
from classes.ssh_client import SshClient
from core import audit, ui
from database import Database
from logs import get_logger

logger = get_logger(__name__)


async def sync_config(ssh: SshClient, db: Database, project_dir: str, remote_folder: str,
                      local_svcs: list, records: list, dry_run: bool = False) -> None:
    env_path = os.path.join(project_dir.rstrip("/"), ".env")
    service_files = [s.name for s in local_svcs]

    options = ["обновить .env (настройки)", "обновить service-файлы (+ daemon-reload)"]
    sel = await ui.checkbox("Что обновить на нодах:", options, default_all=False)
    do_env = 0 in sel
    do_units = 1 in sel
    if not (do_env or do_units):
        print("Ничего не выбрано.")
        return
    if do_env and not os.path.isfile(env_path):
        print(f"🛑 Локальный .env не найден ({env_path}).")
        return

    # целевые ноды = привязанные к программам проекта (где проект реально стоит)
    targets: dict[str, str] = {}
    for rec in records:
        for b in await db.get_service_bindings(rec["program_id"]):
            targets.setdefault(b["ip_address"], b["server_name"] or b["ip_address"])
    if not targets:
        print("Нет привязок в dispatcher.service_status — не на какие ноды обновлять.")
        return
    items = sorted(targets.items(), key=lambda kv: kv[1])

    what = " + ".join(w for w, on in [(".env", do_env), ("юниты", do_units)] if on)
    print(f"\n── Обновление [{what}] на нодах → {remote_folder}{' [DRY-RUN]' if dry_run else ''} ──")
    for ip, name in items:
        print(f"  • {name} ({ip})")
    if not dry_run and not await ui.confirm(
            f"Обновить [{what}] на {len(items)} нод(ах)? Удалённые файлы будут перезаписаны локальными.",
            danger=True):
        print("🛑 Отменено.")
        return

    deployer = Deployer(ssh)
    results = []
    for ip, name in items:
        if not await ssh.ping(ip):
            print(f"  {name:16} 🔌 недоступна — пропускаю")
            results.append((name, ip, False, False))
            continue
        env_ok = (await deployer.sync_env(ip, project_dir, remote_folder, dry_run=dry_run)
                  if do_env else None)
        units_ok = (await deployer.sync_units(ip, project_dir, remote_folder, service_files, dry_run=dry_run)
                    if do_units else None)
        marks = []
        if do_env:
            marks.append("✅ .env" if env_ok else "❌ .env")
        if do_units:
            marks.append("✅ юниты+reload" if units_ok else "❌ юниты")
        tail = " (dry-run)" if dry_run else ""
        print(f"  {name:16} {'  '.join(marks)}{tail}")
        results.append((name, ip, env_ok, units_ok))

    audit.write({
        "action": "sync_config", "ts": datetime.now().isoformat(timespec="seconds"),
        "operator": getpass.getuser(), "project_dir": project_dir, "remote_folder": remote_folder,
        "what": what, "dry_run": dry_run,
        "nodes": [{"node": n, "ip": ip, "env": e, "units": u} for n, ip, e, u in results],
    })

    if dry_run:
        print("\nСухой прогон — изменений не внесено.")
        return
    if await ui.confirm("Перезапустить сервис на нодах (через watchdog), чтобы применить изменения?"):
        from core import watchdog
        await watchdog.manage(db, project_dir, command="restart")
