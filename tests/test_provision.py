from core.provision import _requirement_names, detect_post_install


def _req(tmp_path, content):
    (tmp_path / "requirements.txt").write_text(content)
    return str(tmp_path)


def test_requirement_names(tmp_path):
    names = _requirement_names(_req(tmp_path, "playwright==1.57.0\n# comment\nPillow==12\n"))
    assert "playwright" in names
    assert "pillow" in names


def test_detect_playwright(tmp_path):
    det = detect_post_install(_req(tmp_path, "playwright==1.57.0\nasyncpg~=0.30\n"))
    assert ("playwright", "playwright install firefox") in det


def test_detect_none(tmp_path):
    assert detect_post_install(_req(tmp_path, "asyncpg\nPillow\n")) == []


def test_no_requirements_file(tmp_path):
    assert _requirement_names(str(tmp_path)) == set()
