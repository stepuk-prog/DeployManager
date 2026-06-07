"""Обновление конфигурации на уже развёрнутых нодах БЕЗ полного передеплоя.

Что умеет (по выбору):
  • .env          — залить новый .env (поменялись настройки);
  • service-файлы — обновить юниты в /etc/systemd/system + systemctl daemon-reload.
Цель — ноды, где проект привязан (dispatcher.service_status). Это НЕ обновление кода/версии
(оно — отдельная «ветка обновления»); здесь только конфиг + юниты. После — опц. restart через
watchdog, чтобы изменения применились.
"""
import getpass
import hashlib
import os
import shlex
from datetime import datetime

from classes.deployer import Deployer
from classes.ssh_client import SshClient
from core import audit, ui
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)


def _sha_local(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _sha_remote(ssh: SshClient, ip: str, path: str) -> str | None:
    res = await ssh.run(ip, f"sha256sum -- {shlex.quote(path)} 2>/dev/null", timeout=15)
    parts = res.stdout.split() if res.ok and res.stdout else []
    return parts[0] if parts else None


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
    env_local = _sha_local(env_path) if do_env else None
    units_local = ({sf: _sha_local(os.path.join(project_dir.rstrip("/"), "systemd", sf))
                    for sf in service_files} if do_units else {})
    results = []
    changed_nodes = 0
    for ip, name in items:
        if not await ssh.ping(ip):
            print(f"  {name:16} 🔌 недоступна — пропускаю")
            results.append((name, ip, None, None))
            continue
        marks, env_res, units_res = [], None, None
        if do_env:                                       # сверяем хэш — не трогаем идентичный .env
            if await _sha_remote(ssh, ip, f"{remote_folder.rstrip('/')}/.env") == env_local:
                marks.append("• .env актуален"); env_res = "same"
            else:
                ok = await deployer.sync_env(ip, project_dir, remote_folder, dry_run=dry_run)
                marks.append("✅ .env обновлён" if ok else "❌ .env"); env_res = ok
        if do_units:                                     # сверяем каждый юнит с /etc/systemd/system
            diff = [sf for sf, lh in units_local.items()
                    if await _sha_remote(ssh, ip, f"{config.SYSTEMD_DIR}/{sf}") != lh]
            if not diff:
                marks.append("• юниты актуальны"); units_res = "same"
            else:
                ok = await deployer.sync_units(ip, project_dir, remote_folder, service_files, dry_run=dry_run)
                marks.append(f"✅ юниты+reload ({len(diff)})" if ok else "❌ юниты"); units_res = ok
        if env_res is True or units_res is True:
            changed_nodes += 1
        tail = " (dry-run)" if dry_run else ""
        print(f"  {name:16} {'  '.join(marks)}{tail}")
        results.append((name, ip, env_res, units_res))

    audit.write({
        "action": "sync_config", "ts": datetime.now().isoformat(timespec="seconds"),
        "operator": getpass.getuser(), "project_dir": project_dir, "remote_folder": remote_folder,
        "what": what, "dry_run": dry_run,
        "nodes": [{"node": n, "ip": ip, "env": e, "units": u} for n, ip, e, u in results],
    })

    if dry_run:
        print("\nСухой прогон — изменений не внесено.")
        return
    if not changed_nodes:
        print("\nВсё уже актуально — ничего не меняли, перезапуск не нужен.")
        return
    if await ui.confirm(f"Изменено нод: {changed_nodes}. Перезапустить сервис на них "
                        "(через watchdog), чтобы применить изменения?"):
        from core import watchdog
        await watchdog.manage(ssh, db, project_dir, command="restart")
