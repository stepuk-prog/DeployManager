import json

from classes.manifest import LocalVersion, build_manifest, parse_manifest


def test_parse_none():
    assert parse_manifest(None) is None


def test_parse_bad():
    assert parse_manifest("not json") is None


def test_parse_ok():
    m = parse_manifest('{"commit":"abc","short":"abc"}')
    assert m["commit"] == "abc"


def test_build_roundtrip():
    lv = LocalVersion("a" * 40, "a" * 9, "main", False)
    d = json.loads(build_manifest(lv, "vova", "2026-01-01T00:00:00"))
    assert d["commit"] == "a" * 40
    assert d["branch"] == "main"
    assert d["deployed_by"] == "vova"
    assert d["dirty"] is False


def _git_repo(tmp_path):
    """Мини-монорепо: comp/ (код компонента), other/ (соседний компонент)."""
    import subprocess

    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    shas = []
    for d in ("comp", "other", "other", "comp", "other"):
        (tmp_path / d).mkdir(exist_ok=True)
        f = tmp_path / d / "f.txt"
        f.write_text(f.read_text() + "x" if f.exists() else "x")
        git("add", "-A"); git("commit", "-q", "-m", d)
        shas.append(git("rev-parse", "HEAD"))
    return shas


def test_lag_text_whole_repo_vs_component(tmp_path):
    """Общий репо (Dispatcher2.0): без paths счёт по всему репо, с paths — по коду
    компонента + хвост с репо-счётом (29-09: WD «отставал на 20», по коду — на 1)."""
    from classes.manifest import lag_text
    shas = _git_repo(tmp_path)
    comp = [str(tmp_path / "comp")]
    node, local = shas[0], shas[-1]          # 4 коммита, из них comp/ задел 1
    assert lag_text(str(tmp_path), node, local) == "отстаёт на 4"
    assert lag_text(str(tmp_path), node, local, comp) == "отстаёт на 1 · репо −4"
    # код компонента не менялся (только other/) → «код тот же»
    assert lag_text(str(tmp_path), shas[3], local, comp) == "код тот же · репо −1"
    # все коммиты в диапазоне — по компоненту → хвост не нужен
    assert lag_text(str(tmp_path), shas[2], shas[3], comp) == "отстаёт на 1"
    assert lag_text(str(tmp_path), local, node, comp) == "впереди на 1 · репо +4"
    assert lag_text(str(tmp_path), local, local, comp) == "up-to-date"
