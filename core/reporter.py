"""Деплой Reporter'а на cluster-ноды (кнопка «Reporter»).

Reporter — маленькое приложение, которое по Patroni `on_role_change` → primary шлёт в Telegram
статус кластера. Это НЕ dispatcher-компонент (GD/WD/CD) и НЕ systemd-служба: запускается
Patroni-callback'ом `leader_callback.sh` (/usr/local/bin), крутится под postgres. Поэтому деплой
переиспользует стандартные примитивы (`rsync_project` + `provision`), но со своей спецификой:
источник = Clusters/programs/reporter, владелец = postgres:patroni_group, вместо systemd —
callback + лог-файл, patroni.yml только ПРОВЕРЯЕМ (не правим — дисраптивно на живой ноде).

Разворачивается ТОЛЬКО на cluster-ноды (vocabulary.nodes.claster=true).
"""
import getpass
import os
import shlex

from core import audit, ui
from core.deploy import DeployResult, print_deploy_results
from classes.deployer import Deployer
from classes.ssh_client import SshClient
from database.db import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)


def _src_ok() -> str | None:
    """Проверить, что REPORTER_DIR — реальный проект reporter (main.py, requirements.txt,
    Scripts/leader_callback.sh). Возвращает путь к callback-скрипту или None (с сообщением)."""
    d = config.REPORTER_DIR
    cb = os.path.join(d, "Scripts", "leader_callback.sh")
    for p in (os.path.join(d, "main.py"), os.path.join(d, "requirements.txt"), cb):
        if not os.path.isfile(p):
            print(f"🛑 Нет файла reporter'а: {p} (проверь REPORTER_DIR={d})")
            return None
    return cb


async def _deploy_one(ssh: SshClient, deployer: Deployer, callback_local: str,
                      node, *, dry_run: bool) -> DeployResult:
    ip = node["ip_address"]
    name = node["server_name"] or node["hostname"]
    folder = config.REPORTER_REMOTE
    owner = config.REPORTER_OWNER
    if dry_run:
        print(f"[DRY] {name} ({ip}):")
        print(f"       mkdir -p {folder}/logs; chown vova (для rsync)")
        print(f"       rsync {config.REPORTER_DIR} → {folder}; provision (venv+pip)")
        print(f"       chown -R {owner} {folder}; chmod -R 775")
        print(f"       {callback_local} → {config.REPORTER_CALLBACK} ({owner}, 755) + лог {config.REPORTER_LOG} (664)")
        print(f"       verify {config.PATRONI_YML}: on_role_change → leader_callback.sh")
        return DeployResult(name, ip, True, "dry-run")

    print(f"\n━━━ {name} ({ip}) ━━━")
    fq = shlex.quote(folder)
    # 1. каталог + временно vova-владелец (rsync/provision идут под vova)
    r = await ssh.run_priv(ip, f"mkdir -p {fq}/logs && chown -R {config.SSH_USER}:{config.SSH_USER} {fq}", timeout=30)
    if not r.ok:
        return DeployResult(name, ip, False, "mkdir", r.stderr or r.stdout)
    # 2. код + venv (стандартные примитивы, под vova)
    if not await deployer.rsync_project(ip, config.REPORTER_DIR, folder):
        return DeployResult(name, ip, False, "rsync")
    if not await deployer.provision(ip, folder, []):
        return DeployResult(name, ip, False, "provision")
    # 3. вернуть владельца postgres:patroni_group
    r = await ssh.run_priv(ip, f"chown -R {owner} {fq} && chmod -R 775 {fq}", timeout=30)
    if not r.ok:
        return DeployResult(name, ip, False, "chown", r.stderr or r.stdout)
    # 4. Patroni-callback в /usr/local/bin + лог-файл
    if not await ssh.upload(ip, callback_local, config.REPORTER_CALLBACK, user=config.PRIV_USER, mode=0o755):
        return DeployResult(name, ip, False, "callback-upload")
    cb, log = shlex.quote(config.REPORTER_CALLBACK), shlex.quote(config.REPORTER_LOG)
    r = await ssh.run_priv(
        ip,
        f"chown {owner} {cb} && chmod 755 {cb} && "
        f"touch {log} && chown {owner} {log} && chmod 664 {log}",
        timeout=30)
    if not r.ok:
        return DeployResult(name, ip, False, "callback-perms", r.stderr or r.stdout)
    # 5. verify patroni.yml (НЕ правим — только сообщаем)
    v = await ssh.run_priv(ip, f"grep -q leader_callback.sh {shlex.quote(config.PATRONI_YML)} 2>/dev/null && echo yes || echo no", timeout=15)
    wired = (v.stdout or "").strip() == "yes"
    print(f"   patroni.yml callback: {'✅ прописан' if wired else '⚠️ НЕ найден — впиши on_role_change вручную + patronictl reload'}")
    return DeployResult(name, ip, True, "done", "" if wired else "patroni.yml: callback не прописан")


async def run_reporter(db: Database, ssh: SshClient, *, dry_run: bool = False) -> None:
    """Деплой Reporter'а на все online cluster-ноды. Переиспользует rsync_project+provision."""
    tag = "[DRY] " if dry_run else ""
    callback_local = _src_ok()
    if callback_local is None:
        return
    nodes = [dict(r) for r in await db.get_online_nodes()]
    targets = [n for n in nodes if n.get("claster")]
    skipped = [n for n in nodes if not n.get("claster")]
    if not targets:
        print("🛑 Нет online cluster-нод (claster=true) — Reporter ставится только на кластер.")
        return
    names = ", ".join(n["server_name"] or n["hostname"] for n in targets)
    print(f"{tag}🌐 Reporter → {config.REPORTER_REMOTE} на {len(targets)} cluster-нод(ах): {names}")
    print(f"   источник: {config.REPORTER_DIR} · владелец: {config.REPORTER_OWNER}")
    if skipped:
        print(f"   ⏭️  не-кластерные пропущены ({len(skipped)})")
    if not dry_run and not await ui.confirm(f"Разворачиваю Reporter на {len(targets)} cluster-нод(ах)?"):
        print("Отмена.")
        return

    deployer = Deployer(ssh)
    results = [await _deploy_one(ssh, deployer, callback_local, n, dry_run=dry_run) for n in targets]
    print_deploy_results(results)
    audit.write({
        "action": "reporter", "dry_run": dry_run,
        "targets": [n["server_name"] or n["hostname"] for n in targets],
        "ok": [r.node for r in results if r.ok], "fail": [r.node for r in results if not r.ok],
        "operator": getpass.getuser(),
    })


__all__ = ["run_reporter"]
