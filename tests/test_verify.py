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
