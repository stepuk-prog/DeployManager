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
