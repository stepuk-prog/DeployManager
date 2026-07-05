"""Манифест версии: git-идентичность локального проекта + парсинг VERSION с ноды.

VERSION на сервере — JSON: {commit, short, branch, dirty, deployed_at, deployed_by}.
Сравнение по полю commit (полный SHA) даёт точную «актуальность».
"""
import json
import subprocess
from dataclasses import dataclass


@dataclass
class LocalVersion:
    commit: str          # полный SHA HEAD
    short: str           # короткий SHA
    branch: str
    dirty: bool          # есть незакоммиченные изменения


def _git(project_dir: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:    # нет git / завис
        raise RuntimeError(f"git {' '.join(args)} в {project_dir}: {e}") from e
    if out.returncode != 0:                              # не маскируем сбой пустой строкой
        raise RuntimeError(f"git {' '.join(args)} в {project_dir} → код {out.returncode}: "
                           f"{(out.stderr or '').strip()}")
    return (out.stdout or "").strip()


def local_version(project_dir: str) -> LocalVersion:
    """git-версия локального проекта (бросает, если не git-репозиторий)."""
    commit = _git(project_dir, "rev-parse", "HEAD")
    if not commit:
        raise RuntimeError(f"{project_dir}: не git-репозиторий (нет HEAD)")
    return LocalVersion(
        commit=commit,
        short=commit[:9],
        branch=_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD"),
        dirty=bool(_git(project_dir, "status", "--porcelain")),
    )


def build_manifest(lv: LocalVersion, deployed_by: str, deployed_at: str) -> str:
    """JSON-строка для записи в VERSION на сервере."""
    return json.dumps({
        "commit": lv.commit, "short": lv.short, "branch": lv.branch,
        "dirty": lv.dirty, "deployed_at": deployed_at, "deployed_by": deployed_by,
    }, ensure_ascii=False)


def parse_manifest(text: str | None) -> dict | None:
    """Распарсить VERSION с ноды (или None, если нет/битый)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _rev_count(project_dir: str, rng: str) -> int | None:
    """git rev-list --count <rng> → число или None (коммит не в истории / сбой git)."""
    try:
        out = _git(project_dir, "rev-list", "--count", rng)
    except RuntimeError:
        return None
    return int(out) if out.isdigit() else None


def lag_text(project_dir: str, node_commit: str, local_commit: str) -> str:
    """Текст отставания ноды относительно локальной версии (по git-истории проекта):
    «up-to-date» / «отстаёт на N» / «впереди на N» / «разошлись (−b/+a)» /
    «вне истории репозитория» / «версия неизвестна». Единый источник для дашборда и
    сверки версий (в т.ч. инфра-компонентов) — чтобы вывод был одинаковым."""
    if not node_commit:
        return "версия неизвестна"
    if node_commit == local_commit:
        return "up-to-date"
    behind = _rev_count(project_dir, f"{node_commit}..{local_commit}")
    ahead = _rev_count(project_dir, f"{local_commit}..{node_commit}")
    if behind is None or ahead is None:
        return "вне истории репозитория"
    if behind and not ahead:
        return f"отстаёт на {behind}"
    if ahead and not behind:
        return f"впереди на {ahead}"
    return f"разошлись (−{behind}/+{ahead})"