"""Кнопка «pgBackRest» — копия бэкапа кластера на ЛОКАЛЬНУЮ машину (4-я offsite-копия).

Плановый бэкап (вс 03:00) живёт на 3 cluster-нодах (Clusters/programs/pgbackrest). Эта кнопка
по требованию тянет репозиторий с выбранной cluster-ноды на рабочую станцию оператора в папку
PGBACKREST_LOCAL_DIR (env). Read-only pull (rsync), кластер НЕ трогает. Репо на нодах —
postgres:postgres 750, читаемо только root → тянем под root@node (PRIV_USER).
"""
import getpass
import os

from core import audit, ui
from core.scripts import _run_local            # локальный subprocess со стримингом
from classes.ssh_client import SshClient
from database.db import Database
from settings import config

_PRIV = config.PRIV_USER or "root"


def _ssh_transport() -> str:
    return (f"ssh -i {config.SSH_KEY} -p {config.SSH_PORT} "
            f"-o StrictHostKeyChecking=accept-new -o ConnectTimeout={config.SSH_CONNECT_TIMEOUT}")


async def run_pgbackrest_pull(db: Database, ssh: SshClient, *, dry_run: bool = False) -> None:
    """Скопировать репозиторий pgBackRest с cluster-ноды в PGBACKREST_LOCAL_DIR (локально)."""
    nodes = [dict(r) for r in await db.get_online_nodes()]
    cluster = [n for n in nodes if n.get("claster")]
    if not cluster:
        print("🛑 Нет online cluster-нод (claster=true) — бэкап живёт только на кластере.")
        return
    labels = [f"{n['server_name'] or n['hostname']} ({n['ip_address']})" for n in cluster]
    idx = await ui.select("С какой cluster-ноды скопировать бэкап на эту машину? "
                          "(все ноды синхронны — бери любую живую)", labels)
    if idx is None:
        print("Отмена.")
        return
    node = cluster[idx]
    ip, name = node["ip_address"], node["server_name"] or node["hostname"]
    src = f"{_PRIV}@{ip}:{config.PGBACKREST_REPO}/"
    dst = config.PGBACKREST_LOCAL_DIR
    print(f"📥 pgBackRest: копия {name}:{config.PGBACKREST_REPO} → {dst}")
    if dry_run:
        print(f"[DRY] mkdir -p {dst}; rsync -a --delete {src} {dst}/")
        return

    os.makedirs(dst, exist_ok=True)
    cmd = ["rsync", "-a", "--delete", "-e", _ssh_transport(), src, dst.rstrip("/") + "/"]
    rc = await _run_local(cmd, cwd=dst)
    print("✅ Бэкап скопирован локально." if rc == 0
          else f"⚠️ rsync rc={rc} — проверь root-доступ к {ip} и путь {config.PGBACKREST_REPO}.")
    audit.write({
        "action": "pgbackrest-pull", "node": name, "src": config.PGBACKREST_REPO, "dst": dst,
        "dry_run": dry_run, "rc": rc, "operator": getpass.getuser(),
    })


__all__ = ["run_pgbackrest_pull"]
