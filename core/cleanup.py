"""Пост-проверка после сведения версий (ветка «Проверить версии»): ЛИШНИЕ ФАЙЛЫ.

Файлы, которые есть на ноде, но нет в оригинале (минус деплоимый набор, rsync-исключения,
VERSION и *.log) → чек-бокс → удалить отмеченные. Только на нодах с СОВПАВШЕЙ версией
(иначе «лишнее» неоднозначно: нода может намеренно быть на другой версии).

Пакеты тут НЕ трогаем: новые библиотеки ставит сама синхронизация версии
(`update` → provision: pip install -r), отдельная сверка requirements.txt была бы избыточна.

Работаем только по нодам, где проект развёрнут (есть VERSION в remote_folder).
"""
import shlex

from classes.manifest import parse_manifest
from classes.ssh_client import SshClient
from core import ui
from core.verify import _rsync_excluded, deployed_files
from logs import get_logger
from settings import config

logger = get_logger(__name__)

# Каталоги, которые НЕ обходим при поиске лишних файлов (рантайм/служебное, не «мусор кода»).
_PRUNE_DIRS = ["venv", ".venv", ".git", "__pycache__", ".idea", "node_modules"]


async def _remote_files(ssh: SshClient, host: str, folder: str) -> list[str]:
    """Относительные пути всех файлов на ноде в folder (тяжёлые каталоги не обходим)."""
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in _PRUNE_DIRS)
    cmd = (f"cd {shlex.quote(folder)} && "
           f"find . \\( {prune} \\) -prune -o -type f -print")
    res = await ssh.run(host, cmd, timeout=120)
    files = []
    for line in res.stdout.splitlines():
        p = line.strip()
        if p.startswith("./"):
            p = p[2:]
        if p:
            files.append(p)
    return files


def _stale_files(remote_files: list[str], project_dir: str) -> list[str]:
    """Файлы на ноде, которых нет в оригинале: минус деплоимый набор, rsync-исключения,
    VERSION и *.log."""
    deployed = set(deployed_files(project_dir))
    out = []
    for f in remote_files:
        if f in deployed or f == config.VERSION_FILE:
            continue
        if f.endswith(".log") or _rsync_excluded(f, config.RSYNC_EXCLUDES):
            continue
        out.append(f)
    return sorted(out)


async def _clean_stale(ssh: SshClient, host: str, name: str, folder: str,
                       project_dir: str, dry_run: bool) -> None:
    stale = _stale_files(await _remote_files(ssh, host, folder), project_dir)
    if not stale:
        print(f"  {name}: лишних файлов нет.")
        return
    idxs = await ui.checkbox(
        f"{name}: файлы есть на ноде, но нет в оригинале — отметь к удалению (*.log исключены):",
        stale, default_all=False, dialog_title="Удаление файлов",
        ok_label="🗑️ Удалить", cancel_label="✖️ Отмена", danger=True)
    chosen = [stale[i] for i in idxs]
    if not chosen:
        print(f"  {name}: ничего не выбрано — файлы не трогаю.")
        return
    if dry_run:
        print(f"  {name}: [DRY-RUN] удалил бы {len(chosen)}: {', '.join(chosen)}")
        return
    quoted = " ".join(shlex.quote(f) for f in chosen)
    res = await ssh.run(host, f"cd {shlex.quote(folder)} && rm -f -- {quoted}", timeout=120)
    print(f"  {name}: {'✅ удалено' if res.ok else '❌ ошибка удаления'} {len(chosen)} файл(ов)"
          + ("" if res.ok else f" — {res.stderr or res.stdout}"))


async def post_check(ssh: SshClient, project_dir: str, remote_folder: str,
                     nodes: list, linked_ips: set, local, dry_run: bool = False) -> None:
    """После сведения версий: на каждой развёрнутой online-ноде проекта с совпавшей версией —
    показать лишние файлы (нет в оригинале) и удалить отмеченные."""
    targets = [n for n in nodes if n["ip_address"] in linked_ips]
    if not targets:
        return
    if not await ui.confirm("Проверить ноды на лишние файлы (есть на ноде, нет в оригинале)?"):
        return
    folder = remote_folder.rstrip("/")
    print("\n── Пост-проверка нод: лишние файлы ──")
    for n in targets:
        ip = n["ip_address"]
        name = n["server_name"] or n["hostname"]
        if not await ssh.ping(ip):
            print(f"  {name}: 🔌 недоступна — пропускаю.")
            continue
        man = parse_manifest(await ssh.read_file(ip, f"{folder}/{config.VERSION_FILE}"))
        if man is None:
            print(f"  {name}: проект не развёрнут (нет VERSION) — пропускаю.")
            continue
        if man.get("commit") != local.commit:
            print(f"  {name}: версия не сведена — пропускаю (сначала синхронизируйте).")
            continue
        await _clean_stale(ssh, ip, name, folder, project_dir, dry_run)