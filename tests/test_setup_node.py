"""Тесты «Настроить ноду» (core/setup_node) — чистая логика, без реальных БД/SSH/нод.

Гоняем async-оркестратор через asyncio.run с фейковыми db/ssh и скриптовым ui-бэкендом
(как в test_ui). Главное: гарды (битый IP, дубль, нет скриптов/ключа) и что dry-run
НЕ делает записей (create_node/bootstrap_run/whitelist не зовутся).
"""
import asyncio

from core import setup_node, setup_state, ui
from settings import config


# ── фейки ──
class FakeUi:
    """ui-бэкенд со скриптовыми ответами: asks (очередь), select_idx, confirm_val."""
    def __init__(self, asks, select_idx=0, confirm_val=False):
        self._asks = list(asks)
        self.select_idx = select_idx
        self.confirm_val = confirm_val

    async def ask(self, prompt, default="", **kw):
        return self._asks.pop(0) if self._asks else default

    async def confirm(self, prompt, danger=False, **kw):
        return self.confirm_val

    async def select(self, title, labels, default_index=0, details=None,
                      colors=None, cancel_in_grid=False):
        return self.select_idx


class FakeDb:
    def __init__(self, dup=None):
        self._dup = dup
        self.calls = []

    async def find_node_by_ip(self, ip):
        self.calls.append(("find", ip))
        return self._dup

    async def create_node(self, hostname, ip, server_name, claster=False):
        self.calls.append(("create", ip, hostname, server_name, claster))
        return 999

    async def set_node_online(self, node_id, online=True):
        self.calls.append(("online", node_id, online))

    async def get_online_nodes(self):   # для визарда фазы-2 (член кластера)
        return [
            {"id": 1, "hostname": "cluster1", "server_name": "cluster1",
             "ip_address": "190.2.151.183", "claster": True},
            {"id": 2, "hostname": "cluster2", "server_name": "cluster2",
             "ip_address": "2.58.67.41", "claster": True},
            {"id": 3, "hostname": "n1", "server_name": "NODE-1",
             "ip_address": "2.58.66.56", "claster": False},
        ]


class FakeSsh:
    """Любой вызов ssh в dry-run — ошибка теста (не должно быть)."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def _rec(*a, **kw):
            self.calls.append((name, a))
            raise AssertionError(f"ssh.{name} не должен вызываться в dry-run")
        return _rec


def _scaffold(tmp_path, monkeypatch, *, scripts=True, pubkey=True):
    """Разложить tmp bundled-scripts/* и SSH_KEY(+.pub); подменить BUNDLED_DIR (скрипты
    ВЕНДОРЕНЫ в assets/fleet_scripts) + config. scripts=False → пустой dir (гард «нет скрипта»)."""
    from core import scripts as scripts_mod
    sdir = tmp_path / "scripts"
    sdir.mkdir()
    if scripts:
        for f in ("provision-base.sh", "provision-client.sh", "whitelist-ip.sh",
                  "pw_lock_sweep.sh"):
            (sdir / f).write_text("#!/usr/bin/env bash\n")
    key = tmp_path / "id_nodes"
    key.write_text("PRIVATE")
    if pubkey:
        (tmp_path / "id_nodes.pub").write_text("ssh-ed25519 AAAA vova@ws\n")
    monkeypatch.setattr(scripts_mod, "BUNDLED_DIR", str(sdir))
    monkeypatch.setattr(config, "SSH_KEY", str(key))
    # изолировать resume-журнал в tmp (не писать в реальный logs/setup_node/)
    monkeypatch.setattr(setup_state, "_DIR", str(tmp_path / "journal"))


def _run(db, ssh, ui_backend, monkeypatch):
    audit_rec = {}
    monkeypatch.setattr(setup_node.audit, "write", lambda rec: audit_rec.update(rec))
    ui.set_backend(ui_backend)
    try:
        asyncio.run(setup_node.run_setup_node(db, ssh, dry_run=True))
    finally:
        ui.set_backend(None)
    return audit_rec


# ── тесты ──
def test_missing_scripts_guard(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch, scripts=False)
    db = FakeDb()
    _run(db, FakeSsh(), FakeUi(["1.2.3.4"]), monkeypatch)
    assert db.calls == []                       # до БД не дошли


def test_missing_pubkey_guard(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch, pubkey=False)
    db = FakeDb()
    _run(db, FakeSsh(), FakeUi(["1.2.3.4"]), monkeypatch)
    assert db.calls == []


def test_invalid_ip_aborts(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch)
    db = FakeDb()
    _run(db, FakeSsh(), FakeUi(["не-ip"]), monkeypatch)
    assert db.calls == []                        # даже дубль-гард не трогали


def test_duplicate_ip_idempotent(tmp_path, monkeypatch):
    # Новая семантика: дубль в БД НЕ абортит мастер (шаг register идемпотентен). На первом
    # запуске find зовётся для префилла hostname/server_name; в dry-run create не вызывается.
    _scaffold(tmp_path, monkeypatch)
    db = FakeDb(dup={"id": 5, "server_name": "OLD", "hostname": "old"})
    _run(db, FakeSsh(), FakeUi(["1.2.3.4", "n-node8", "N8", "pw"]), monkeypatch)
    assert ("find", "1.2.3.4") in db.calls
    assert not any(c[0] == "create" for c in db.calls)   # dry-run не пишет


def test_dry_run_no_writes(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch)
    db = FakeDb(dup=None)
    ssh = FakeSsh()
    # ordinary-узел: select_idx=0; ответы формы по порядку
    audit_rec = _run(db, ssh, FakeUi(["1.2.3.4", "n-node8", "N8", "pw"], select_idx=0),
                     monkeypatch)
    assert ("find", "1.2.3.4") in db.calls       # дубль-гард отработал
    assert not any(c[0] == "create" for c in db.calls)   # НЕ пишем в БД в dry-run
    assert not any(c[0] == "online" for c in db.calls)
    assert ssh.calls == []                        # ssh не трогали в dry-run
    assert audit_rec.get("dry_run") is True
    assert audit_rec.get("type") == "client"


def test_cancel_on_form_aborts(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch)
    db = FakeDb(dup=None)
    # None из ask = нажата «Отмена» на поле IP → мастер выходит, БД не трогаем
    _run(db, FakeSsh(), FakeUi([None]), monkeypatch)
    assert db.calls == []


def test_journal_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_state, "_DIR", str(tmp_path / "j"))
    assert setup_state.load("7.7.7.7") is None
    j = setup_state.create("7.7.7.7", "n7", "N7", "client")
    assert setup_state.load("7.7.7.7")["hostname"] == "n7"
    assert not setup_state.is_done(j, "user")
    setup_state.set_step(j, "user", setup_state.DONE)
    setup_state.set_field(j, "node_id", 213)
    reloaded = setup_state.load("7.7.7.7")
    assert reloaded["steps"]["user"] == "done"
    assert reloaded["node_id"] == 213
    assert setup_state.is_done(reloaded, "user")


def test_resume_asks_only_ip(tmp_path, monkeypatch):
    # Журнал уже есть → resume: спрашиваем ТОЛЬКО IP, данные берём из журнала (не переспрашиваем).
    _scaffold(tmp_path, monkeypatch)
    setup_state.create("9.9.9.9", "n-node9", "N9", "client")
    db = FakeDb(dup=None)
    audit_rec = _run(db, FakeSsh(), FakeUi(["9.9.9.9"]), monkeypatch)  # в очереди только IP
    assert audit_rec.get("hostname") == "n-node9"      # из журнала, не спрошено
    assert audit_rec.get("type") == "client"
    assert not any(c[0] == "find" for c in db.calls)   # resume не префиллит через find


def test_cluster_branch_enters_wizard(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch)
    db = FakeDb(dup=None)
    # select_idx=1 → «Элемент кластера» → визард фазы-2; confirm_val=False → отмена на подтверждении
    # топологии. Регистрация (create_node) в визарде — ручной шаг 8, авто НЕ вызывается.
    _run(db, FakeSsh(), FakeUi(["1.2.3.4", "n-node8", "N8", "pw"], select_idx=1, confirm_val=False),
         monkeypatch)
    assert not any(c[0] == "create" for c in db.calls)
