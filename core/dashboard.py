"""Read-only дашборд: программы проекта → ноды (status/running) → версия + отставание.

Источники: programdata (программы по service-файлам), dispatcher.service_status
(привязки leader/standby + running), VERSION на ноде (SSH). Отставание считается через
git rev-list между SHA ноды и локальным (если оба в истории проекта).
"""
import subprocess

from classes.manifest import parse_manifest
from classes.ssh_client import SshClient
from core.validate import list_local_services
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)


def _count(project_dir: str, rng: str) -> int | None:
    r = subprocess.run(["git", "-C", project_dir, "rev-list", "--count", rng],
                       capture_output=True, text=True, timeout=15)
    s = r.stdout.strip()
    return int(s) if r.returncode == 0 and s.isdigit() else None


def _lag(project_dir: str, node_commit: str, local_commit: str) -> str:
    """Текст отставания ноды относительно локальной версии."""
    if not node_commit:
        return "версия неизвестна"
    if node_commit == local_commit:
        return "up-to-date"
    behind = _count(project_dir, f"{node_commit}..{local_commit}")
    ahead = _count(project_dir, f"{local_commit}..{node_commit}")
    if behind is None or ahead is None:
        return "вне истории репозитория"
    if behind and not ahead:
        return f"отстаёт на {behind}"
    if ahead and not behind:
        return f"впереди на {ahead}"
    return f"разошлись (−{behind}/+{ahead})"


async def show(ssh: SshClient, db: Database, project_dir: str, local) -> None:
    svcs = list_local_services(project_dir)
    names = [s.name for s in svcs if not s.is_template]
    records = await db.find_programs_by_service(names)
    print(f"\n══ Дашборд проекта · программ: {len(records)} · локально {local.short} ({local.branch})"
          f"{' DIRTY' if local.dirty else ''} ══")
    if not records:
        print("  Программы проекта не найдены в programdata.")
        return
    for rec in records:
        folder = (rec["folder"] or "").rstrip("/")
        bindings = await db.get_service_bindings(rec["program_id"])
        print(f"\n▸ {rec['service_name']}  [id={rec['program_id']} · "
              f"dispatcher={'on' if rec['dispatcher'] else 'off'} · "
              f"активна={'да' if rec['status'] else 'нет'} · {folder or 'путь?'}]")
        if not bindings:
            print("    — нет привязок в dispatcher.service_status")
            continue
        for b in bindings:
            ip = b["ip_address"]
            node = b["server_name"] or ip
            man = parse_manifest(await ssh.read_file(ip, f"{folder}/{config.VERSION_FILE}")) if folder else None
            if man is None:
                vinfo = "нет VERSION"
            else:
                nc = man.get("commit", "")
                vinfo = f"{man.get('short') or nc[:9]} · {_lag(project_dir, nc, local.commit)}"
            run = "▶ running" if b["running"] else "■ stopped"
            print(f"    {node:16} {b['status']:11} {run:10} {vinfo}")