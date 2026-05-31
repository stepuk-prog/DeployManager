"""Пост-деплой хэш-сверка: файлы на ноде идентичны локальным (целостность rsync).

Сверяется набор реально задеплоенных файлов = git-tracked минус невыгружаемые (`*.md`,
`.env.example`, `pictures/new/`) плюс `.env` (он деплоится, но не в git). По каждому файлу
sha256 локально и на ноде (`sha256sum`), сравнение.
"""
import hashlib
import os
import shlex
import subprocess

from classes.ssh_client import SshClient
from logs import get_logger

logger = get_logger(__name__)


def deployed_files(project_dir: str) -> list[str]:
    """Относительные пути файлов, которые попадают на сервер (для сверки)."""
    out = subprocess.run(["git", "-C", project_dir, "ls-files"],
                         capture_output=True, text=True, timeout=15).stdout
    files = []
    for f in out.splitlines():
        f = f.strip()
        if not f:
            continue
        if f.endswith(".md") or os.path.basename(f) == ".env.example" or f.startswith("pictures/new/"):
            continue
        files.append(f)
    if os.path.isfile(os.path.join(project_dir, ".env")):
        files.append(".env")
    return sorted(set(files))


def local_hashes(project_dir: str, files: list[str]) -> dict[str, str]:
    result = {}
    for f in files:
        p = os.path.join(project_dir, f)
        if not os.path.isfile(p):
            continue
        digest = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        result[f] = digest.hexdigest()
    return result


async def remote_hashes(ssh: SshClient, host: str, folder: str, files: list[str]) -> dict[str, str]:
    """sha256 файлов на ноде. Отсутствующие файлы просто не попадут в результат."""
    quoted = " ".join(shlex.quote(f) for f in files)
    res = await ssh.run(host, f"cd {shlex.quote(folder.rstrip('/'))} && sha256sum -- {quoted}", timeout=180)
    result = {}
    for line in res.stdout.splitlines():   # парсим stdout даже при ненулевом коде (часть файлов могла отсутствовать)
        parts = line.split(None, 1)
        if len(parts) == 2:
            result[parts[1].strip()] = parts[0].strip()
    return result


def compare(local: dict[str, str], remote: dict[str, str]) -> list[tuple[str, str]]:
    """[(файл, 'ok'|'DIFFER'|'missing')] для всех локальных файлов."""
    out = []
    for f, lh in local.items():
        rh = remote.get(f)
        out.append((f, "ok" if rh == lh else ("missing" if rh is None else "DIFFER")))
    return out


async def verify_node(ssh: SshClient, host: str, folder: str, project_dir: str) -> tuple[int, int, list[str]]:
    """Возвращает (всего, совпало, [проблемные файлы])."""
    files = deployed_files(project_dir)
    local = local_hashes(project_dir, files)
    remote = await remote_hashes(ssh, host, folder, files)
    results = compare(local, remote)
    ok = sum(1 for _, st in results if st == "ok")
    bad = [f"{f} [{st}]" for f, st in results if st != "ok"]
    return len(results), ok, bad
