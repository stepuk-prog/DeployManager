"""Пост-проверка после сведения версий (ветка «Проверить версии»): ЛИШНИЕ ФАЙЛЫ.

Файлы, которые есть на ноде, но нет в оригинале (минус деплоимый набор, rsync-исключения,
VERSION и *.log) → чек-бокс → удалить отмеченные. Только на нодах с СОВПАВШЕЙ версией
(иначе «лишнее» неоднозначно: нода может намеренно быть на другой версии).

Пакеты тут НЕ трогаем: новые библиотеки ставит сама синхронизация версии
(`update` → provision: pip install -r), отдельная сверка requirements.txt была бы избыточна.

Работаем только по нодам, где проект развёрнут (есть VERSION в remote_folder).
"""
import asyncio
import os
import shlex

from classes.manifest import parse_manifest
from classes.ssh_client import SshClient
from core import ui
from core.verify import _rsync_excluded
from logs import get_logger
from settings import config

logger = get_logger(__name__)

# Каталоги, которые НЕ обходим при поиске лишних файлов (рантайм/служебное, не «мусор кода»).
_PRUNE_DIRS = ["venv", ".venv", ".git", "__pycache__", ".idea", ".claude", ".vscode", "node_modules"]

# Dev-артефакты редакторов/инструментов: исключены из деплоя (RSYNC_EXCLUDES), но могли остаться
# на ноде от прошлых деплоев — предлагаем снести целиком (каталог/файл), вне зависимости от git.
_PURGE_NAMES = [".claude", ".vscode", ".idea", ".directory"]


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
    """Файлы на ноде, которых НЕТ в локальном проекте. Сверяем по факту наличия файла в
    project_dir (источник истины — rsync, а он копирует и gitignored-файлы), а НЕ по git:
    иначе .claude и прочее «вне git», но реально лежащее в проекте, ложно считалось бы лишним.
    Минус rsync-исключения, VERSION и *.log."""
    out = []
    for f in remote_files:
        if f == config.VERSION_FILE or f.endswith(".log"):
            continue
        if _rsync_excluded(f, config.RSYNC_EXCLUDES):
            continue
        if os.path.exists(os.path.join(project_dir, f)):
            continue                       # файл есть в оригинале → не лишний
        out.append(f)
    return sorted(out)


async def _purge_present(ssh: SshClient, host: str, folder: str) -> list[str]:
    """Какие dev-артефакты из _PURGE_NAMES реально лежат на ноде (верхний уровень folder)."""
    names = " ".join(shlex.quote(n) for n in _PURGE_NAMES)
    cmd = (f"cd {shlex.quote(folder)} && "
           f'for n in {names}; do [ -e "$n" ] && echo "$n"; done')
    res = await ssh.run(host, cmd, timeout=20)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


async def _detect(ssh: SshClient, node, folder: str, project_dir: str, local) -> dict:
    """Read-only обнаружение лишнего на одной ноде (выполняется параллельно). Возвращает
    {name, skip} (пропуск) либо {name, ip, junk, stale} (к интерактивному удалению)."""
    ip = node["ip_address"]
    name = node["server_name"] or node["hostname"]
    if not await ssh.ping(ip):
        return {"name": name, "skip": "🔌 недоступна"}
    man = parse_manifest(await ssh.read_file(ip, f"{folder}/{config.VERSION_FILE}"))
    if man is None:
        return {"name": name, "skip": "проект не развёрнут (нет VERSION)"}
    if man.get("commit") != local.commit:
        return {"name": name, "skip": "версия не сведена — сначала синхронизируйте"}
    junk = await _purge_present(ssh, ip, folder)                          # dev-артефакты целиком
    stale = _stale_files(await _remote_files(ssh, ip, folder), project_dir)  # удалённые файлы
    return {"name": name, "ip": ip, "junk": junk, "stale": stale}


async def _prompt_delete(ssh: SshClient, host: str, name: str, folder: str,
                         junk: list[str], stale: list[str], dry_run: bool) -> None:
    """Интерактив (последовательно, модальные диалоги): чек-бокс лишнего → rm -rf отмеченного."""
    candidates = junk + stale
    if not candidates:
        print(f"  {name}: лишнего нет.")
        return
    labels = [f"{j}  (dev-артефакт — снести целиком)" for j in junk] + stale
    idxs = await ui.checkbox(
        f"{name}: лишнее на ноде — отметь к удалению (*.log/рантайм исключены):",
        labels, default_all=False, dialog_title="Удаление файлов",
        ok_label="🗑️ Удалить", cancel_label="✖️ Отмена", danger=True)
    chosen = [candidates[i] for i in idxs]
    if not chosen:
        print(f"  {name}: ничего не выбрано — не трогаю.")
        return
    if dry_run:
        print(f"  {name}: [DRY-RUN] удалил бы {len(chosen)}: {', '.join(chosen)}")
        return
    quoted = " ".join(shlex.quote(f) for f in chosen)
    res = await ssh.run(host, f"cd {shlex.quote(folder)} && rm -rf -- {quoted}", timeout=120)
    print(f"  {name}: {'✅ удалено' if res.ok else '❌ ошибка удаления'} {len(chosen)} шт."
          + ("" if res.ok else f" — {res.stderr or res.stdout}"))


async def post_check(ssh: SshClient, project_dir: str, remote_folder: str,
                     nodes: list, linked_ips: set, local, dry_run: bool = False) -> None:
    """После сведения версий: на каждой развёрнутой online-ноде проекта с совпавшей версией —
    показать лишние файлы (нет в оригинале) и удалить отмеченные. Обнаружение по нодам —
    параллельно (read-only SSH), удаление — последовательно (модальные чек-боксы)."""
    targets = [n for n in nodes if n["ip_address"] in linked_ips]
    if not targets:
        return
    if not await ui.confirm("Проверить ноды на лишние файлы (есть на ноде, нет в оригинале)?"):
        return
    folder = remote_folder.rstrip("/")
    print("\n── Пост-проверка нод: лишние файлы ──")
    total = len(targets)
    done = {"n": 0}
    ui.progress(f"Проверка нод: 0/{total}…")

    async def _d(n):
        f = await _detect(ssh, n, folder, project_dir, local)
        done["n"] += 1
        ui.progress(f"Проверка нод: {done['n']}/{total}…")
        return f

    findings = await asyncio.gather(*[_d(n) for n in targets])
    ui.progress("")
    for f in findings:                       # интерактив — последовательно
        if f.get("skip"):
            print(f"  {f['name']}: {f['skip']} — пропускаю.")
            continue
        await _prompt_delete(ssh, f["ip"], f["name"], folder, f["junk"], f["stale"], dry_run)