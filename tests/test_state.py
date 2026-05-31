from core.state import parse_systemctl_show


def test_active():
    s = parse_systemctl_show("LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n")
    assert s.running is True
    assert s.error is None


def test_failed():
    s = parse_systemctl_show("LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n")
    assert s.running is False
    assert s.error == "failed/exit-code"


def test_inactive_not_error():
    s = parse_systemctl_show("LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n")
    assert s.running is False
    assert s.error is None


def test_not_found():
    s = parse_systemctl_show("LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n")
    assert s.running is False
    assert "not-found" in s.error