"""Git push (core/gitpush.py): состояние репо, поиск секретов, push только fast-forward, гейт деплоя.

Всё на временных репозиториях: «origin» — локальный bare-репо, сеть не нужна.
"""
import asyncio
import subprocess

import pytest

from core import gitpush, ui


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, name, text, msg="c"):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def pair(tmp_path):
    """(work, origin): рабочий репо master с одним коммитом, связанный с bare origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "master")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "a.py", "x = 1\n")
    _git(work, "push", "-q", "-u", "origin", "master")
    return work, origin


@pytest.fixture(autouse=True)
def _noninteractive():
    ui.set_mode(interactive=False, assume_yes=True)
    yield
    ui.set_mode(interactive=True)


def test_https_to_ssh():
    assert (gitpush.https_to_ssh("https://github.com/stepuk-prog/Clusters.git", "gh-alias")
            == "git@gh-alias:stepuk-prog/Clusters.git")
    assert (gitpush.https_to_ssh("https://github.com/stepuk-prog/Clusters", "gh-alias")
            == "git@gh-alias:stepuk-prog/Clusters.git")
    assert gitpush.https_to_ssh("git@github.com:o/r.git", "gh-alias") is None
    assert gitpush.https_to_ssh("https://gitlab.com/o/r.git", "gh-alias") is None
    assert gitpush.https_to_ssh("https://github.com/o/r.git", "") is None


def test_find_repos_skips_archive_and_venv(tmp_path):
    for d in ("A", "Group/B", "_archive/Old", "C/venv/pkg"):
        (tmp_path / d).mkdir(parents=True)
        _git(tmp_path / d, "init", "-q")
    (tmp_path / "A" / "nested").mkdir()
    _git(tmp_path / "A" / "nested", "init", "-q")      # в найденный репо не спускаемся
    found = [p.replace(str(tmp_path) + "/", "") for p in gitpush.find_repos(str(tmp_path))]
    assert found == ["A", "Group/B"]


def test_state_clean_then_ahead(pair):
    work, _ = pair
    st = gitpush.repo_state(str(work), fetch=True)
    assert (st.branch, st.upstream, st.ahead, st.behind, st.dirty) == ("master", "origin/master", 0, 0, 0)
    assert not st.needs_push
    _commit(work, "b.py", "y = 2\n")
    (work / "untracked.txt").write_text("z")
    st = gitpush.repo_state(str(work))
    assert (st.ahead, st.dirty) == (1, 1) and st.needs_push


def test_state_no_upstream_counts_unpushed(pair):
    work, _ = pair
    _git(work, "checkout", "-q", "-b", "feature")
    _commit(work, "f.py", "f = 1\n")
    st = gitpush.repo_state(str(work))
    assert st.upstream is None and st.branch == "feature" and st.ahead == 1


def test_scan_secrets_finds_env_and_token(pair):
    work, _ = pair
    _commit(work, ".env", "DB=1\n")
    _commit(work, ".env.example", "DB=\n")
    _commit(work, "cfg.py", "TOKEN = '123456789:" + "A" * 35 + "'\n")
    found = gitpush.scan_secrets(gitpush.repo_state(str(work)))
    assert any(f == "файл .env" for f in found)
    assert not any(".env.example" in f for f in found)
    assert any(f.startswith("токен Telegram-бота") for f in found)


def test_scan_secrets_clean(pair):
    work, _ = pair
    _commit(work, "b.py", "def f():\n    return 1\n")
    assert gitpush.scan_secrets(gitpush.repo_state(str(work))) == []


def test_push_fast_forward(pair):
    work, origin = pair
    _commit(work, "b.py", "y = 2\n")
    assert asyncio.run(gitpush.push_repo(gitpush.repo_state(str(work))))
    assert _git(origin, "rev-parse", "master") == _git(work, "rev-parse", "HEAD")


def test_push_refused_when_behind(pair, tmp_path):
    work, origin = pair
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _commit(other, "o.py", "o = 1\n")
    _git(other, "push", "-q")
    _commit(work, "b.py", "y = 2\n")                  # разошлись: ahead 1, behind 1
    st = gitpush.repo_state(str(work), fetch=True)
    assert (st.ahead, st.behind) == (1, 1)
    before = _git(origin, "rev-parse", "master")
    assert not asyncio.run(gitpush.push_repo(st))
    assert _git(origin, "rev-parse", "master") == before   # origin не тронут, без --force


def test_ensure_pushed_clean_passes(pair):
    work, _ = pair
    assert asyncio.run(gitpush.ensure_pushed(str(work)))


def test_ensure_pushed_pushes_by_default(pair):
    work, origin = pair
    _commit(work, "b.py", "y = 2\n")
    # неинтерактив: select → default_index=0 «Запушить и продолжить»
    assert asyncio.run(gitpush.ensure_pushed(str(work)))
    assert _git(origin, "rev-parse", "master") == _git(work, "rev-parse", "HEAD")


def test_ensure_pushed_not_git(tmp_path):
    assert asyncio.run(gitpush.ensure_pushed(str(tmp_path)))
