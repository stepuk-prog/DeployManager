from core.verify import compare


def test_compare_statuses():
    local = {"a": "1", "b": "2", "c": "3"}
    remote = {"a": "1", "b": "XX"}  # c отсутствует, b отличается
    res = dict(compare(local, remote))
    assert res["a"] == "ok"
    assert res["b"] == "DIFFER"
    assert res["c"] == "missing"


def test_compare_all_ok():
    h = {"x": "deadbeef", "y": "cafe"}
    assert all(st == "ok" for _, st in compare(h, dict(h)))


# ── рабочие кадры не уезжают на ноду (RSYNC_EXCLUDES, 22-09-2026) ────────────────────────────
from core.verify import _rsync_excluded          # noqa: E402  (тест ниже — про сам матчер)
from settings import config                      # noqa: E402


def test_working_frames_excluded():
    """Кадры, которые программа пишет НА НОДЕ, локальными копиями не перетираем: `screenshot*`
    уходит подписчику в момент отправки, `shot*` — сырой кадр до оверлеев."""
    for rel in ("pictures/screenshot.png", "pictures/screenshot_1m_otc.png",
                "pictures/screenshot_OTC_GBP_AUD.png", "pictures/shot.png",
                "pictures/shot_new_0.png"):
        assert _rsync_excluded(rel, config.RSYNC_EXCLUDES), rel


def test_production_assets_kept():
    """Рядом с кадрами лежат боевые ассеты — их исключение задевать не должно."""
    for rel in ("pictures/qr-code_110.png", "pictures/start_week.png", "pictures/globe_otc.png",
                "pictures/end_week.png", "pictures/seria_plus.png", "pictures/bug.png",
                "apps/app.py", "messages/message.py"):
        assert not _rsync_excluded(rel, config.RSYNC_EXCLUDES), rel


# ── исключения проекта (.deployignore, 06-10-2026) ─────────────────────────────────────────
def test_deployignore_anchored_root_dirs(tmp_path):
    """`/scripts` и `/tests` из .deployignore режут только каталоги в КОРНЕ проекта, а не
    одноимённые вложенные (у Dispatcher2.0/Clusters `scripts` — рабочий каталог)."""
    (tmp_path / ".deployignore").write_text("# локальные инструменты\n/scripts\n\n/tests\n", encoding="utf-8")
    excludes = config.project_excludes(str(tmp_path))
    assert excludes == ["/scripts", "/tests"]
    for rel in ("scripts/channel_corpus.py", "scripts/__pycache__/x.pyc", "tests/test_intent.py", "scripts"):
        assert _rsync_excluded(rel, excludes), rel
    for rel in ("apps/scripts/run.py", "database/voice_assistant.sql", "main.py", "testsuite/x.py"):
        assert not _rsync_excluded(rel, excludes), rel


def test_deployignore_absent(tmp_path):
    assert config.project_excludes(str(tmp_path)) == []
