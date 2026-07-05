"""Read-only дашборд: программы проекта → ноды (status/running) → версия + отставание.

Источники: programdata (программы по service-файлам), dispatcher.service_status
(привязки leader/standby + running), VERSION на ноде (SSH). Отставание считается через
git rev-list между SHA ноды и локальным (если оба в истории проекта).
"""
import asyncio
import subprocess

from classes.manifest import parse_manifest
from classes.ssh_client import SshClient
from core import ui
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


async def show(ssh: SshClient, db: Database, project_dir: str, local) -> list[dict]:
    """Печатает дашборд и возвращает список отставших/разошедшихся нод (версия != локальной)
    в формате [{ip, name, commit, lag}] — для предложения синхронизации."""
    svcs = list_local_services(project_dir)
    names = [s.name for s in svcs if not s.is_template]
    records = await db.find_programs_by_service(names)
    print(f"\n══ Дашборд проекта · программ: {len(records)} · локально {local.short} ({local.branch})"
          f"{' DIRTY' if local.dirty else ''} ══")
    if not records:
        print("  Программы проекта не найдены в programdata.")
        return []
    ui.progress("Опрос версий на нодах…")
    # привязки всех записей заранее (нужны и для версий по нодам, и для перечня сервисов)
    binds: dict[int, list] = {rec["program_id"]: await db.get_service_bindings(rec["program_id"])
                              for rec in records}
    # группируем по folder: одна папка = один код = одна версия на ноду (не по каждому сервису)
    groups: dict[str, list] = {}
    for rec in records:
        groups.setdefault((rec["folder"] or "").rstrip("/"), []).append(rec)

    stale: dict[str, dict] = {}                       # ip → инфо (одна нода — один раз)
    lag_cache: dict[str, str] = {}                    # node_commit → текст отставания (git rev-list)
    for folder, recs in groups.items():
        # объединяем ноды всех сервисов папки (версия читается по ноде, а не по сервису)
        nodes_by_ip: dict[str, str] = {}
        for rec in recs:
            for b in binds[rec["program_id"]]:
                nodes_by_ip.setdefault(b["ip_address"], b["server_name"] or b["ip_address"])
        # ── версии по нодам: один раз на папку (один код = одна версия на ноду) ──
        print(f"\nКод: {folder or '(folder не задан в programdata)'}")
        if not folder:
            print("  версия неизвестна — путь установки не указан")
        elif not nodes_by_ip:
            print("  — нет привязок в dispatcher.service_status")
        else:
            ips = list(nodes_by_ip)
            mans = await asyncio.gather(*[_read_manifest(ssh, ip, folder) for ip in ips])
            for ip, man in zip(ips, mans):
                node = nodes_by_ip[ip]
                if man is None:
                    print(f"  🔌 {node:16} нет VERSION")
                    continue
                nc = man.get("commit", "")
                if nc not in lag_cache:
                    lag_cache[nc] = _lag(project_dir, nc, local.commit)
                lag = lag_cache[nc]
                icon = "✅" if lag == "up-to-date" else "⚠️"
                print(f"  {icon} {node:16} {lag:18} {man.get('short') or nc[:9]}")
                if nc and nc != local.commit and ip not in stale:
                    stale[ip] = {"ip": ip, "name": node, "commit": nc, "lag": lag}
        # ── сервисы этой папки: только реестр имён (детальное состояние/leader — в сводке
        #    «Проверка состояния сервисов» выше; здесь не повторяем список нод по каждому юниту) ──
        disp = sum(1 for r in recs if r["dispatcher"])
        roster = ", ".join(r["service_name"] for r in recs)
        print(f"Сервисы ({len(recs)}, под диспетчером {disp}): {roster}")
    ui.progress("")
    return list(stale.values())


async def _read_manifest(ssh: SshClient, ip: str, folder: str) -> dict | None:
    return parse_manifest(await ssh.read_file(ip, f"{folder}/{config.VERSION_FILE}"))