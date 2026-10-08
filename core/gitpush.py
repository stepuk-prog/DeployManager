"""Git push проектов: обзор всех репозиториев PROJECTS_DIR и гейт деплоя.

Два входа:
- `run(db)` — инструмент «📤 Git push» (реестр tools/): находит все git-репозитории под PROJECTS_DIR
  (кроме GIT_SKIP_DIRS — `_archive`, venv и т.п.), делает fetch, показывает ветку / незапушено /
  отстаёт / незакоммичено / https-remote и пушит выбранные чек-боксами.
- `ensure_pushed(project_dir)` — гейт перед выкаткой кода на ноды (деплой, обновление отставших,
  инфра-компоненты): если выкатываемый HEAD не на GitHub — «Запушить и продолжить / Деплоить без
  push / Отмена». Тогда `VERSION` на ноде ссылается на коммит, который есть в origin.

Push — только fast-forward (никакого --force): отстающую от origin ветку не пушим, просим pull.
Перед push — проверка выкатываемых коммитов на секреты (имена файлов + добавленные строки); при
находке — подтверждение с danger. https-remote на GitHub предлагается перевести на ssh-алиас
GIT_SSH_HOST_ALIAS (из терминала https без логина не пушится). Незакоммиченное не трогаем:
DM ничего не коммитит, пушит только то, что уже в коммитах.
"""
import asyncio
import getpass
import os
import re
import subprocess
from dataclasses import dataclass

from core import audit, ui
from settings import config

_FETCH_PARALLEL = 8          # одновременных `git fetch` в обзоре
_FETCH_TIMEOUT = 40          # сек на fetch одного репо
_PUSH_TIMEOUT = 120          # сек на push

# Имена файлов, которым не место в GitHub (сравнение по пути внутри репо).
_SECRET_NAME_RE = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|[^/]*\.session(-journal)?|\.pgpass|id_(rsa|ed25519|ecdsa)[^/]*"
    r"|[^/]*storage_state[^/]*\.json|[^/]*cookies[^/]*\.json)$", re.IGNORECASE)
_SECRET_NAME_OK_RE = re.compile(r"\.(example|sample|template|dist)$", re.IGNORECASE)
# Содержимое добавленных строк: токен бота, приватный ключ, ключ AWS, пароль/секрет литералом.
_SECRET_LINE_RES = (
    ("токен Telegram-бота", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("приватный ключ", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("ключ AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("пароль/секрет литералом", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api_?key|token)\b\s*[=:]\s*['\"][^'\"\s]{6,}['\"]")),
)
_HTTPS_GITHUB_RE = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+?)(\.git)?/?$")


@dataclass
class RepoState:
    path: str
    branch: str = ""            # пусто — detached HEAD
    upstream: str | None = None  # 'origin/master' или None (ветка не связана с origin)
    ahead: int = 0              # коммитов не в upstream (или всего коммитов, если upstream нет)
    behind: int = 0             # коммитов upstream, которых нет локально
    dirty: int = 0              # незакоммиченных путей (git status --porcelain)
    remote_url: str = ""        # origin
    fetch_error: str = ""       # fetch не прошёл (сеть/доступ) — ahead/behind по старому состоянию
    error: str = ""             # репо не читается

    @property
    def needs_push(self) -> bool:
        return not self.error and bool(self.branch) and bool(self.remote_url) and self.ahead > 0

    @property
    def https(self) -> bool:
        return self.remote_url.startswith("https://")


def _git(cwd: str, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    """git -C cwd args → (код, stdout, stderr). Сбой запуска/таймаут → код 124/127, без исключения.
    GIT_TERMINAL_PROMPT=0: https без кредов падает сразу, а не виснет на запросе логина."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"таймаут {timeout} с"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def find_repos(root: str, skip: tuple[str, ...] = config.GIT_SKIP_DIRS) -> list[str]:
    """Корни git-репозиториев под root (в найденный репо не спускаемся). Отсортировано."""
    found = []
    for dirpath, dirnames, _files in os.walk(root):
        if ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git")):
            found.append(dirpath)
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
    return sorted(found)


def repo_state(path: str, fetch: bool = False) -> RepoState:
    """Состояние репозитория. fetch=True — сперва `git fetch origin` (свежие ahead/behind)."""
    st = RepoState(path=path)
    rc, out, err = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        st.error = err or "не git-репозиторий"
        return st
    st.branch = "" if out == "HEAD" else out
    _rc, st.remote_url, _ = _git(path, "remote", "get-url", "origin")
    if fetch and st.remote_url:
        rc, _out, err = _git(path, "fetch", "-q", "origin", timeout=_FETCH_TIMEOUT)
        if rc != 0:
            st.fetch_error = (err.splitlines() or ["fetch не прошёл"])[-1][:160]
    rc, up, _ = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    st.upstream = up if rc == 0 and up else None
    if st.upstream:
        rc, out, _ = _git(path, "rev-list", "--left-right", "--count", f"{st.upstream}...HEAD")
        if rc == 0 and out:
            behind, ahead = (int(x) for x in out.split())
            st.behind, st.ahead = behind, ahead
    elif st.branch:
        # ветка не связана с origin: «незапушено» = коммиты, которых нет ни в одной origin/*
        rc, out, _ = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes=origin")
        st.ahead = int(out) if rc == 0 and out.isdigit() else 0
    rc, out, _ = _git(path, "status", "--porcelain")
    st.dirty = len(out.splitlines()) if rc == 0 and out else 0
    return st


def https_to_ssh(url: str, alias: str = config.GIT_SSH_HOST_ALIAS) -> str | None:
    """https://github.com/<owner>/<repo>[.git] → git@<alias>:<owner>/<repo>.git; иначе None."""
    m = _HTTPS_GITHUB_RE.match(url or "")
    if not m or not alias:
        return None
    return f"git@{alias}:{m.group(1)}/{m.group(2)}.git"


def _push_range(st: RepoState) -> str:
    return f"{st.upstream}..HEAD" if st.upstream else "HEAD --not --remotes=origin"


def scan_secrets(st: RepoState) -> list[str]:
    """Подозрения на секреты в коммитах, которые уйдут push'ем: файлы по имени + добавленные строки."""
    rng = _push_range(st).split()
    found: list[str] = []
    rc, out, _ = _git(st.path, "log", "--format=", "--name-only", "--diff-filter=AM", *rng)
    if rc == 0:
        for name in sorted(set(filter(None, out.splitlines()))):
            if _SECRET_NAME_RE.search(name) and not _SECRET_NAME_OK_RE.search(name):
                found.append(f"файл {name}")
    rc, out, _ = _git(st.path, "log", "-p", "--format=", "--no-color", *rng, timeout=60)
    if rc == 0:
        seen: set[str] = set()
        for line in out.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for label, rx in _SECRET_LINE_RES:
                if rx.search(line) and label not in seen:
                    seen.add(label)
                    found.append(f"{label}: {line[1:].strip()[:100]}")
    return found


def _rel(path: str) -> str:
    try:
        rel = os.path.relpath(path, config.PROJECTS_DIR)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def _describe(st: RepoState) -> str:
    """Строка обзора: имя, ветка, незапушено/отстаёт/незакоммичено, https."""
    if st.error:
        return f"{_rel(st.path)} — ❌ {st.error}"
    bits = [st.branch or "detached HEAD"]
    if not st.remote_url:
        bits.append("без origin")
    elif not st.upstream:
        bits.append("ветка не связана с origin")
    if st.ahead:
        bits.append(f"незапушено {st.ahead}")
    if st.behind:
        bits.append(f"отстаёт {st.behind}")
    if st.dirty:
        bits.append(f"незакоммичено {st.dirty}")
    if st.https:
        bits.append("https")
    if st.fetch_error:
        bits.append(f"fetch: {st.fetch_error}")
    return f"{_rel(st.path)} — " + ", ".join(bits)


async def push_repo(st: RepoState) -> bool:
    """Запушить текущую ветку (fast-forward). Все проверки — внутри; True — запушено."""
    name = _rel(st.path)
    if not st.branch:
        print(f"🛑 {name}: detached HEAD — пушить нечего (переключись на ветку).")
        return False
    if not st.remote_url:
        print(f"🛑 {name}: нет remote origin.")
        return False
    if st.behind:
        print(f"🛑 {name}: ветка отстаёт от {st.upstream} на {st.behind} — сначала pull/rebase, "
              f"push без --force не пройдёт (DM не форсит).")
        return False
    if st.dirty:
        print(f"   {name}: незакоммичено {st.dirty} — уйдут только коммиты, рабочие файлы не трогаю.")
    if st.https:
        ssh_url = https_to_ssh(st.remote_url)
        if ssh_url and await ui.confirm(
                f"{name}: origin по https ({st.remote_url}) — из терминала без логина не пушится.\n"
                f"Перевести на ssh: {ssh_url}?"):
            rc, _o, err = _git(st.path, "remote", "set-url", "origin", ssh_url)
            if rc != 0:
                print(f"⚠️ {name}: set-url не прошёл: {err}")
                return False
            print(f"   {name}: origin → {ssh_url}")
            st.remote_url = ssh_url
    secrets = scan_secrets(st)
    if secrets:
        lines = "\n   ".join(secrets[:12])
        if not await ui.confirm(f"⚠️ {name}: в коммитах на push похоже на секреты:\n   {lines}\n"
                                f"Всё равно пушить?", danger=True):
            print(f"   {name}: push отменён (секреты).")
            return False
    args = ["push", "origin", f"HEAD:refs/heads/{st.branch}"]
    if not st.upstream:
        if not await ui.confirm(f"{name}: ветка {st.branch} не связана с origin — создать "
                                f"origin/{st.branch} и связать (push -u)?"):
            print(f"   {name}: push отменён.")
            return False
        args = ["push", "-u", "origin", st.branch]
    rc, out, err = _git(st.path, *args, timeout=_PUSH_TIMEOUT)
    ok = rc == 0
    tail = (err or out).splitlines()
    print(f"{'✅' if ok else '❌'} {name}: " + (tail[-1] if tail else ("запушено" if ok else f"код {rc}")))
    audit.write({"action": "git-push", "repo": st.path, "branch": st.branch, "commits": st.ahead,
                 "ok": ok, "rc": rc, "operator": getpass.getuser(),
                 "secrets_warned": len(secrets)})
    return ok


async def ensure_pushed(project_dir: str) -> bool:
    """Гейт перед выкаткой кода: HEAD на GitHub? True — продолжать деплой, False — отмена."""
    st = await asyncio.to_thread(repo_state, project_dir, True)
    if st.error or not st.remote_url or not st.branch:
        return True                      # не git / без origin / detached — гейт не применим
    if st.fetch_error:
        print(f"   ⚠️ git fetch не прошёл ({st.fetch_error}) — сверяю с последним известным origin.")
    if not st.ahead:
        return True
    where = f"не в {st.upstream}" if st.upstream else "ветка не связана с origin"
    idx = await ui.select(
        f"Выкатываемая версия не на GitHub: {st.ahead} коммит(ов) {where} ({_describe(st)}).\n"
        f"После деплоя VERSION на нодах будет ссылаться на коммит, которого нет в origin.",
        ["Запушить и продолжить", "Деплоить без push", "Отмена"], default_index=0,
        colors=["green", "blue", "red"])
    if idx == 0:
        if await push_repo(st):
            return True
        return await ui.confirm("Push не прошёл. Деплоить без push?")
    return idx == 1


async def run(db=None) -> None:
    """Инструмент «📤 Git push»: обзор репозиториев PROJECTS_DIR → push выбранных."""
    root = config.PROJECTS_DIR
    repos = await asyncio.to_thread(find_repos, root)
    if not repos:
        print(f"Репозиториев под {root} не найдено.")
        return
    print(f"📤 Git push: {len(repos)} репозиториев под {root} (без {', '.join(config.GIT_SKIP_DIRS)}), "
          f"git fetch…")
    sem = asyncio.Semaphore(_FETCH_PARALLEL)

    async def one(path: str) -> RepoState:
        async with sem:
            return await asyncio.to_thread(repo_state, path, True)

    states = await asyncio.gather(*(one(p) for p in repos))
    todo = [s for s in states if s.needs_push]
    for s in states:
        if s.needs_push or s.behind or s.dirty or s.https or s.error or s.fetch_error:
            print(f"  {'📤' if s.needs_push else '  '} {_describe(s)}")
    clean = len(states) - sum(1 for s in states
                              if s.needs_push or s.behind or s.dirty or s.https or s.error or s.fetch_error)
    print(f"  … чистых и синхронных с origin: {clean}")
    if not todo:
        print("✅ Пушить нечего — все коммиты на GitHub.")
        return
    picked = await ui.checkbox("Что запушить?", [_describe(s) for s in todo],
                               default_checked=[not s.behind for s in todo],
                               ok_label="Запушить", cancel_label="Отмена",
                               dialog_title="Git push")
    if not picked:
        print("Ничего не выбрано.")
        return
    results = [await push_repo(todo[i]) for i in picked]
    print(f"Итог: запушено {sum(results)} из {len(results)}.")


__all__ = ["RepoState", "find_repos", "repo_state", "https_to_ssh", "scan_secrets",
           "push_repo", "ensure_pushed", "run"]
