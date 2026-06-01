"""Ветка обновления: синхронизация уже развёрнутых нод до текущей локальной версии.

В отличие от первичного деплоя (preflight пропускает ноды с VERSION как «уже развёрнуты»),
здесь мы НАМЕРЕННО перезаписываем отставшие/разошедшиеся ноды: rsync кода → provision →
install_services → write_version (через deploy_mod.deploy), затем restart через watchdog,
чтобы новый код применился. Цель — ноды из dashboard (версия != локальной).
"""
import getpass
from datetime import datetime

from classes.deployer import Deployer
from classes.ssh_client import SshClient
from core import audit, deploy as deploy_mod, ui
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)


async def update(ssh: SshClient, db: Database, project_dir: str, remote_folder: str,
                 local_svcs: list, records: list, local, nodes: list,
                 stale: list[dict], dry_run: bool = False) -> None:
    """stale — [{ip, name, commit, lag}] из dashboard.show."""
    stale_ips = {s["ip"] for s in stale}
    targets = [n for n in nodes if n["ip_address"] in stale_ips]
    offline = stale_ips - {n["ip_address"] for n in targets}
    if offline:
        print(f"⚠️ Вне online-нод (пропускаю): {', '.join(sorted(offline))}")
    if not targets:
        print("Нет доступных online-нод для синхронизации.")
        return
    service_files = [s.name for s in local_svcs]

    lag_by_ip = {s["ip"]: s["lag"] for s in stale}
    print(f"\n── Синхронизация версии → {local.short} ({local.branch})"
          f"{' DIRTY' if local.dirty else ''}{' [DRY-RUN]' if dry_run else ''} ──")
    for n in targets:
        name = n["server_name"] or n["hostname"]
        print(f"  • {name:16} ({n['ip_address']})  сейчас: {lag_by_ip.get(n['ip_address'], '?')}")
    if local.dirty:
        print("  ⚠️⚠️ Локально есть незакоммиченные изменения (DIRTY) — версия на ноде будет неточной.")
    if not dry_run and not await ui.confirm(
            f"Обновить код на {len(targets)} нод(ах) до локальной {local.short} и перезапустить? "
            "Работающие сервисы будут перезапущены.", danger=True):
        print("🛑 Отменено.")
        return

    # При обновлении пост-установки (playwright install и т.п.) НЕ предлагаем: браузер уже стоит
    # на ноде с первичного деплоя. provision сделает только venv + pip install -r (если изменились
    # зависимости). Переустановка тяжёлых пакетов — отдельно, при первичном деплое/add.
    results = await deploy_mod.deploy(
        ssh, Deployer(ssh), targets, project_dir, remote_folder, service_files, local,
        deployed_by=getpass.getuser(), deployed_at=datetime.now().isoformat(timespec="seconds"),
        extra_cmds=[], dry_run=dry_run)
    deploy_mod.print_deploy_results(results)

    if dry_run:
        print("\nСухой прогон — версия не менялась.")
        return

    operator = getpass.getuser()
    for rec in records:
        for n, res in zip(targets, results):
            await db.journal_write(
                rec["program_id"], n["id"], "update",
                folder_deployed=res.step not in ("ping", "rsync"),
                service_installed=res.step in ("write_version", "done"), db_updated=res.ok,
                result=("ok" if res.ok else res.step), commit=local.commit, operator=operator,
                details={"ip": n["ip_address"], "detail": res.detail})
    audit.write({
        "action": "update", "ts": datetime.now().isoformat(timespec="seconds"),
        "operator": operator, "project_dir": project_dir, "remote_folder": remote_folder,
        "commit": local.commit, "short": local.short,
        "nodes": [{"node": r.node, "ip": r.ip, "ok": r.ok, "step": r.step} for r in results],
    })

    if any(r.ok for r in results) and await ui.confirm(
            "Перезапустить сервис на обновлённых нодах (через watchdog), чтобы применить новый код?"):
        from core import watchdog
        await watchdog.manage(db, project_dir, command="restart")
