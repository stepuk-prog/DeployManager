from core.dashboard import _lag


def test_lag_equal(tmp_path):
    assert _lag(str(tmp_path), "abc123", "abc123") == "up-to-date"


def test_lag_unknown_version(tmp_path):
    assert _lag(str(tmp_path), "", "abc123") == "версия неизвестна"


def test_lag_out_of_history(tmp_path):
    # tmp_path — не git-репозиторий: rev-list не сработает → «вне истории»
    assert _lag(str(tmp_path), "deadbeef", "cafebabe") == "вне истории репозитория"
