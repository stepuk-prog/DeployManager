"""Пост-проверка после сведения версий (ветка «Проверить версии»):

  1) ЛИШНИЕ ФАЙЛЫ — есть на ноде, но нет в оригинале (минус деплоимый набор, rsync-исключения,
     VERSION и *.log) → чек-бокс → удалить отмеченные. Только на нодах с СОВПАВШЕЙ версией
     (иначе «лишнее» неоднозначно: нода может намеренно быть на другой версии).
  2) requirements.txt РАЗОШЁЛСЯ с локальным → предложить обновить пакеты на ноде
     (доставить requirements.txt + pip install -r в venv ноды).

Работаем только по нодам, где проект развёрнут (есть VERSION в remote_folder).
"""
import base64
import os
import shlex

from classes.deployer import Deployer
from classes.manifest import parse_manifest
from classes.ssh_client import SshClient
from core import ui
from core.verify import _rsync_excluded, deployed_files, local_hashes, remote_hashes
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


async def _check_requirements(ssh: SshClient, host: str, name: str, folder: str,
                              project_dir: str, dry_run: bool) -> None:
    """requirements.txt на ноде vs локально; при расхождении — доставить и pip install -r."""
    lh = local_hashes(project_dir, ["requirements.txt"])
    if "requirements.txt" not in lh:
        return                                   # у проекта нет requirements.txt — нечего сверять
    rh = await remote_hashes(ssh, host, folder, ["requirements.txt"])
    if rh.get("requirements.txt") == lh["requirements.txt"]:
        print(f"  {name}: requirements.txt совпадает.")
        return
    print(f"  {name}: ⚠️ requirements.txt расходится с локальным.")
    if not await ui.confirm(
            f"{name}: обновить пакеты (pip install -r requirements.txt) на ноде?", danger=True):
        print(f"  {name}: пакеты не трогаю.")
        return
    if dry_run:
        print(f"  {name}: [DRY-RUN] доставил бы requirements.txt и обновил пакеты.")
        return
    # Доставляем актуальный requirements.txt и ставим зависимости в venv ноды.
    with open(os.path.join(project_dir, "requirements.txt"), "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    dst = shlex.quote(os.path.join(folder, "requirements.txt"))
    push = await ssh.run(host, f"sh -c {shlex.quote(f'echo {b64} | base64 -d > {dst}')}", timeout=30)
    if not push.ok:
        print(f"  {name}: ❌ не удалось доставить requirements.txt — {push.stderr or push.stdout}")
        return
    ok = await Deployer(ssh).provision(host, folder, [])
    print(f"  {name}: {'✅ пакеты обновлены' if ok else '❌ ошибка обновления пакетов (см. лог)'}.")


async def post_check(ssh: SshClient, project_dir: str, remote_folder: str,
                     nodes: list, linked_ips: set, local, dry_run: bool = False) -> None:
    """После сведения версий: по каждой развёрнутой online-ноде проекта — чистка лишних файлов
    (на нодах с совпавшей версией) и сверка requirements.txt (с предложением обновить пакеты)."""
    targets = [n for n in nodes if n["ip_address"] in linked_ips]
    if not targets:
        return
    if not await ui.confirm("Проверить ноды на лишние файлы и расхождение requirements.txt?"):
        return
    folder = remote_folder.rstrip("/")
    print("\n── Пост-проверка нод (лишние файлы / requirements) ──")
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
        if man.get("commit") == local.commit:
            await _clean_stale(ssh, ip, name, folder, project_dir, dry_run)   # версия сведена
        else:
            print(f"  {name}: версия не сведена — чистку лишних файлов пропускаю.")
        await _check_requirements(ssh, ip, name, folder, project_dir, dry_run)
