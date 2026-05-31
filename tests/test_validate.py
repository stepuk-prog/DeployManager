from core.validate import parse_service_file

UNIT = """[Unit]
Description=Test
[Service]
WorkingDirectory=/home/vova/Proj
ExecStart=/home/vova/Proj/{venv}/bin/python3.11 main.py
Restart={restart}
"""


def _write(tmp_path, venv="venv", restart="no", name="x.service"):
    p = tmp_path / name
    p.write_text(UNIT.format(venv=venv, restart=restart))
    return str(p)


def test_parse_workdir_and_venv(tmp_path):
    svc = parse_service_file(_write(tmp_path))
    assert svc.working_dir == "/home/vova/Proj"
    assert svc.venv_dir == "venv"


def test_restart_enabled(tmp_path):
    assert parse_service_file(_write(tmp_path, restart="on-failure")).restart_enabled is True
    assert parse_service_file(_write(tmp_path, restart="always")).restart_enabled is True
    assert parse_service_file(_write(tmp_path, restart="no")).restart_enabled is False


def test_restart_absent_is_disabled(tmp_path):
    p = tmp_path / "y.service"
    p.write_text("[Service]\nExecStart=/a/venv/bin/python m.py\n")
    assert parse_service_file(str(p)).restart_enabled is False


def test_venv_dotvenv_detected(tmp_path):
    assert parse_service_file(_write(tmp_path, venv=".venv")).venv_dir == ".venv"


def test_template_flag(tmp_path):
    assert parse_service_file(_write(tmp_path, name="bot@.service")).is_template is True
    assert parse_service_file(_write(tmp_path, name="x.service")).is_template is False
