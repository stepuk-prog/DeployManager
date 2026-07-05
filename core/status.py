"""Проверка актуальности версий проекта на нодах (VERSION-манифест vs локальный git SHA)."""
import asyncio
from dataclasses import dataclass

from classes.manifest import LocalVersion, lag_text, parse_manifest
from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)


@dataclass
class NodeStatus:
    node: str
    ip: str
    state: str           # up-to-date | stale | missing | unreachable | dirty-local
    remote_short: str    # короткий SHA на ноде ('—' если нет)
    lag: str = ""        # «отстаёт на N» / «впереди на N» / «разошлись …» (пусто, если неактуально)
    note: str = ""


async def _one(ssh: SshClient, node, remote_folder: str, local: LocalVersion,
               project_dir: str | None) -> NodeStatus:
    ip = node["ip_address"]
    name = node["server_name"] or node["hostname"]
    if not await ssh.ping(ip):
        return NodeStatus(name, ip, "unreachable", "—", note="нет SSH")
    raw = await ssh.read_file(ip, f"{remote_folder.rstrip('/')}/{config.VERSION_FILE}")
    man = parse_manifest(raw)
    if man is None:
        return NodeStatus(name, ip, "missing", "—",
                          note="нет VERSION (не деплоился этим инструментом)")
    remote_commit = man.get("commit", "")
    short = (man.get("short") or remote_commit[:9]) or "—"
    if remote_commit == local.commit:
        state = "dirty-local" if local.dirty else "up-to-date"
        note = "локально есть незакоммиченные правки" if local.dirty else ""
        return NodeStatus(name, ip, state, short, lag="up-to-date", note=note)
    # отстаёт/впереди/разошлись — счётчик коммитов по git-истории (как в стандартном дашборде)
    lag = lag_text(project_dir, remote_commit, local.commit) if project_dir else ""
    return NodeStatus(name, ip, "stale", short, lag=lag,
                      note=f"ветка {man.get('branch','?')} @ {short}")


async def check_status(ssh: SshClient, nodes: list, remote_folder: str,
                       local: LocalVersion, project_dir: str | None = None) -> list[NodeStatus]:
    """Параллельно опросить ноды и сравнить с локальной версией. project_dir (git-репо проекта)
    — чтобы посчитать отставание в коммитах («отстаёт на N»); без него счётчик пуст."""
    results = await asyncio.gather(
        *[_one(ssh, n, remote_folder, local, project_dir) for n in nodes])
    return list(results)


def print_status(local: LocalVersion, statuses: list[NodeStatus]) -> None:
    icon = {"up-to-date": "✅", "stale": "⚠️", "missing": "❓",
            "unreachable": "🔌", "dirty-local": "✅*"}
    print(f"\nЛокальная версия: {local.short} ({local.branch})"
          f"{'  ⚠️ DIRTY (незакоммичено)' if local.dirty else ''}")
    print(f"{'НОДА':18} {'IP':16} {'СОСТОЯНИЕ':14} {'SHA ноды':11} {'ОТСТАВАНИЕ':18} ПРИМЕЧАНИЕ")
    print("-" * 104)
    for s in statuses:
        lag = "" if s.lag in ("", "up-to-date") else s.lag
        print(f"{s.node:18} {s.ip:16} {icon.get(s.state,'?')} {s.state:11} "
              f"{s.remote_short:11} {lag:18} {s.note}")
