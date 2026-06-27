"""Ветка «Проверить версии»: ЛИШНИЕ ФАЙЛЫ (`post_check`) и ОЧИСТКА ЛОГОВ (`clear_logs`).

Файлы, которые есть на ноде, но нет в оригинале (минус деплоимый набор, rsync-исключения,
VERSION и *.log) → чек-бокс → удалить отмеченные. Только на нодах с СОВПАВШЕЙ версией
(иначе «лишнее» неоднозначно: нода может намеренно быть на другой версии).

Отдельно `clear_logs` (вызывается ПЕРЕД проверкой лишних файлов): обнулить *.log на нодах
(truncate -s 0, безопасно для работающих сервисов) — см. блок ниже.

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
# `.git.backup` — отдельно от `.git`: это полноценная git-папка-бэкап, без prune find насыпал бы
# десятки её внутренних файлов в список «лишнего».
_PRUNE_DIRS = ["venv", ".venv", ".git", ".git.backup", "__pycache__", ".idea", ".claude",
               ".vscode", "node_modules"]

# Dev-артефакты редакторов/инструментов: исключены из деплоя (RSYNC_EXCLUDES), но могли остаться
# на ноде от прошлых деплоев — предлагаем снести целиком (каталог/файл), вне зависимости от git.
# `.git.backup` — тоже сюда: rsync его раньше не исключал (паттерн `.git` его не ловит) → утекал.
_PURGE_NAMES = [".claude", ".vscode", ".idea", ".directory", ".git.backup"]


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


# ─────────────────────────────── очистка логов ───────────────────────────────
# Отдельная опция в ветке «Проверить версии» (перед проверкой лишних файлов): обнулить
# *.log на нодах. Чистим truncate'ом (-s 0), а НЕ rm: у работающего сервиса открытый
# дескриптор остаётся валидным — процесс продолжает писать, место освобождается сразу
# (после rm место не вернулось бы до рестарта — процесс держит «призрачный» inode).
# Версию НЕ сверяем (в отличие от лишних файлов): чистка логов не зависит от того, на какой
# версии нода, — достаточно, что проект развёрнут (есть VERSION).

def _human(n: int) -> str:
    """Человекочитаемый размер (B/K/M/G/T)."""
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1024 or unit == "T":
            return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}T"


async def _remote_logs(ssh: SshClient, host: str, folder: str) -> list[tuple[str, int]]:
    """Относительные пути и размеры всех *.log в folder (тяжёлые каталоги не обходим)."""
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in _PRUNE_DIRS)
    cmd = (f"cd {shlex.quote(folder)} && "
           f"find . \\( {prune} \\) -prune -o -type f -name '*.log' -printf '%s\\t%p\\n'")
    res = await ssh.run(host, cmd, timeout=120)
    out = []
    for line in res.stdout.splitlines():
        if "\t" not in line:
            continue
        size, p = line.split("\t", 1)
        p = p.strip()
        if p.startswith("./"):
            p = p[2:]
        if p and size.strip().isdigit():
            out.append((p, int(size)))
    return sorted(out)


async def _detect_logs(ssh: SshClient, node, folder: str) -> dict:
    """Read-only обнаружение *.log на одной ноде (параллельно). Возвращает {name, skip}
    либо {name, ip, logs:[(path,size)]}."""
    ip = node["ip_address"]
    name = node["server_name"] or node["hostname"]
    if not await ssh.ping(ip):
        return {"name": name, "skip": "🔌 недоступна"}
    man = parse_manifest(await ssh.read_file(ip, f"{folder}/{config.VERSION_FILE}"))
    if man is None:
        return {"name": name, "skip": "проект не развёрнут (нет VERSION)"}
    return {"name": name, "ip": ip, "logs": await _remote_logs(ssh, ip, folder)}


async def _prompt_clear(ssh: SshClient, host: str, name: str, folder: str,
                        logs: list[tuple[str, int]], dry_run: bool) -> None:
    """Интерактив (последовательно): чек-бокс *.log (предотмечены все) → truncate -s 0."""
    if not logs:
        print(f"  {name}: логов (*.log) нет.")
        return
    labels = [f"{p}  ({_human(sz)})" for p, sz in logs]
    idxs = await ui.checkbox(
        f"{name}: логи (*.log) — отметь к обнулению (truncate -s 0):",
        labels, default_all=True, dialog_title="Очистка логов",
        ok_label="🧹 Обнулить", cancel_label="✖️ Отмена", danger=True)
    if not idxs:
        print(f"  {name}: ничего не выбрано — не трогаю.")
        return
    chosen = [logs[i][0] for i in idxs]
    freed = sum(logs[i][1] for i in idxs)
    if dry_run:
        print(f"  {name}: [DRY-RUN] обнулил бы {len(chosen)} ({_human(freed)}): {', '.join(chosen)}")
        return
    quoted = " ".join(shlex.quote(p) for p in chosen)
    cmd = f"cd {shlex.quote(folder)} && truncate -s 0 -- {quoted}"
    res = await ssh.run(host, cmd, timeout=120)
    # Часть логов пишет сервис под root → файл root-owned, vova не может truncate
    # ('Permission denied'). truncate -s 0 не меняет владельца → повтор под root безопасен.
    if not res.ok and "ermission denied" in (res.stderr or res.stdout):
        res = await ssh.run_priv(host, cmd, timeout=120)
    print(f"  {name}: {'✅ обнулено' if res.ok else '❌ ошибка'} {len(chosen)} шт. ({_human(freed)})"
          + ("" if res.ok else f" — {res.stderr or res.stdout}"))


async def clear_logs(ssh: SshClient, remote_folder: str, nodes: list,
                     linked_ips: set, dry_run: bool = False) -> None:
    """Опц. очистка логов: на каждой развёрнутой online-ноде проекта показать *.log и обнулить
    отмеченные (truncate -s 0). Обнаружение по нодам — параллельно, чистка — последовательно."""
    targets = [n for n in nodes if n["ip_address"] in linked_ips]
    if not targets:
        return
    if not await ui.confirm("Очистить логи (*.log) на нодах проекта (truncate -s 0)?"):
        return
    folder = remote_folder.rstrip("/")
    print("\n── Очистка логов нод (*.log) ──")
    total = len(targets)
    done = {"n": 0}
    ui.progress(f"Поиск логов: 0/{total}…")

    async def _d(n):
        f = await _detect_logs(ssh, n, folder)
        done["n"] += 1
        ui.progress(f"Поиск логов: {done['n']}/{total}…")
        return f

    findings = await asyncio.gather(*[_d(n) for n in targets])
    ui.progress("")
    for f in findings:                       # интерактив — последовательно
        if f.get("skip"):
            print(f"  {f['name']}: {f['skip']} — пропускаю.")
            continue
        await _prompt_clear(ssh, f["ip"], f["name"], folder, f["logs"], dry_run)