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
    out = subprocess.run(
        ["git", "-C", project_dir, *args],
        capture_output=True, text=True, timeout=15,
    )
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