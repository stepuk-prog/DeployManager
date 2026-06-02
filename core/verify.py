"""Пост-деплой хэш-сверка: файлы на ноде идентичны локальным (целостность rsync).

Сверяется набор реально задеплоенных файлов = git-tracked минус то, что rsync исключает
(`config.RSYNC_EXCLUDES` — те же правила, что при выгрузке: `logs/*`, `files/*`, `*.md`, …),
плюс `.env` (он деплоится, но не в git). По каждому файлу sha256 локально и на ноде
(`sha256sum`), сравнение. Список исключений ОБЯЗАН совпадать с rsync — иначе невыгруженные
файлы дают ложные `[missing]`.
"""
import fnmatch
import hashlib
import os
import shlex
import subprocess

from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)


def _rsync_excluded(rel: str, excludes: list[str]) -> bool:
    """Попадает ли путь под правило исключения rsync (та же семантика, что у `--exclude`)."""
    parts = rel.split("/")
    for pat in excludes:
        if "/" in pat:                                  # anchored: путь от корня переноса
            if fnmatch.fnmatch(rel, pat):               # logs/* → logs/__init__.py, logs/sub/x.py
                return True
            if rel == pat or rel.startswith(pat + "/"):  # каталог целиком (pictures/new и его содержимое)
                return True
        elif any(fnmatch.fnmatch(p, pat) for p in parts):  # unanchored: любой компонент (*.md, __pycache__)
            return True
    return False


def deployed_files(project_dir: str) -> list[str]:
    """Относительные пути файлов, которые попадают на сервер (для сверки).
    Берём git-tracked И untracked (не игнорируемые .gitignore) — последнее это новые исходники,
    ещё не закоммиченные, но которые rsync всё равно доставит. Игнорируемые (.gitignore) не берём
    (логи/venv/кэши — их и rsync режет через RSYNC_EXCLUDES). Затем фильтр RSYNC_EXCLUDES."""
    def _git_ls(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", project_dir, "ls-files", *args],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"git ls-files {' '.join(args)} в {project_dir}: {e}") from e
        if r.returncode != 0:                            # не git-репо / ошибка — не молчим
            raise RuntimeError(f"git ls-files в {project_dir} → код {r.returncode}: {(r.stderr or '').strip()}")
        return r.stdout
    tracked = _git_ls()
    untracked = _git_ls("--others", "--exclude-standard")
    includes = set(config.RSYNC_INCLUDES)          # вернулись через --include, несмотря на exclude
    files = []
    for f in (tracked.splitlines() + untracked.splitlines()):
        f = f.strip()
        if not f or (_rsync_excluded(f, config.RSYNC_EXCLUDES) and f not in includes):
            continue
        files.append(f)
    if os.path.isfile(os.path.join(project_dir, ".env")) and not _rsync_excluded(".env", config.RSYNC_EXCLUDES):
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
    if not files:                                        # иначе `sha256sum --` зависнет на stdin
        return {}
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


async def verify_node(ssh: SshClient, host: str, folder: str, project_dir: str,
                      ignore_globs: list[str] | None = None) -> tuple[int, int, list[str]]:
    """Возвращает (всего, совпало, [проблемные файлы]). ignore_globs — пути (fnmatch), которые
    исключаются из сверки (напр. ['systemd/*.service'] при решении «совпал ли КОД», когда юниты
    как раз доустанавливаются и их расхождение/отсутствие не должно считаться рассинхроном)."""
    files = deployed_files(project_dir)
    if ignore_globs:
        files = [f for f in files if not any(fnmatch.fnmatch(f, g) for g in ignore_globs)]
    local = local_hashes(project_dir, files)
    remote = await remote_hashes(ssh, host, folder, files)
    results = compare(local, remote)
    ok = sum(1 for _, st in results if st == "ok")
    bad = [f"{f} [{st}]" for f, st in results if st != "ok"]
    return len(results), ok, bad
