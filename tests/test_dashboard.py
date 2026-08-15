import asyncio
import json

from classes.ssh_client import CmdResult
from core.dashboard import (VER_MISSING, VER_OK, VER_UNREACHABLE, _lag, _read_manifest)


class _FakeSsh:
    """SSH-заглушка: отдаёт заранее заданный CmdResult на любой run()."""

    def __init__(self, result: CmdResult):
        self._result = result

    async def run(self, host, command, timeout=30, sudo=False, user=None):
        return self._result


def _read(result: CmdResult):
    return asyncio.run(_read_manifest(_FakeSsh(result), "1.2.3.4", "/opt/app"))


def test_manifest_ok():
    payload = json.dumps({"commit": "abc123", "short": "abc123"})
    state, man = _read(CmdResult(True, 0, payload, ""))
    assert state == VER_OK and man["commit"] == "abc123"


def test_manifest_missing_file():
    # cat не нашёл файл: код 1 — нода жива, версии нет → синхронизировать
    state, man = _read(CmdResult(False, 1, "", "No such file or directory"))
    assert state == VER_MISSING and man is None


def test_manifest_unreachable():
    # 255 ставит SshClient.run на таймаут/обрыв — нода не ответила
    state, man = _read(CmdResult(False, 255, "", "timeout 15s"))
    assert state == VER_UNREACHABLE and man is None


def test_manifest_broken_json():
    # файл есть, но содержимое битое — версия всё равно неизвестна
    state, man = _read(CmdResult(True, 0, "{не json", ""))
    assert state == VER_MISSING and man is None


def test_lag_equal(tmp_path):
    assert _lag(str(tmp_path), "abc123", "abc123") == "up-to-date"


def test_lag_unknown_version(tmp_path):
    assert _lag(str(tmp_path), "", "abc123") == "версия неизвестна"


def test_lag_out_of_history(tmp_path):
    # tmp_path — не git-репозиторий: rev-list не сработает → «вне истории»
    assert _lag(str(tmp_path), "deadbeef", "cafebabe") == "вне истории репозитория"
