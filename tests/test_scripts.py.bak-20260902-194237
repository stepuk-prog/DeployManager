"""Тесты реестра доп-скриптов (core/scripts) — чистая логика, без реальных БД/SSH/нод.

Гоняем run_script через asyncio.run с фейковыми db/ssh, скриптовым ui-бэкендом и
подменённым _run_local (локальный subprocess не запускаем). Проверяем: apply-гейт
(dry-run→confirm→apply), отмену ввода, валидацию IP, node-scope (пикер+upload+run),
сбор argv (позиционный/со значением/булев).
"""
import asyncio

import pytest

from classes.ssh_client import CmdResult
from core import scripts, ui
from settings import config


# ── фейки ──
class FakeUi:
    """asks (очередь), confirms (очередь bool), select_idx."""
    def __init__(self, asks=(), confirms=(), select_idx=0):
        self._asks = list(asks)
        self._confirms = list(confirms)
        self.select_idx = select_idx

    async def ask(self, prompt, default="", **kw):
        return self._asks.pop(0) if self._asks else default

    async def confirm(self, prompt, danger=False, **kw):
        return self._confirms.pop(0) if self._confirms else False

    async def select(self, title, labels, default_index=0, details=None,
                     colors=None, cancel_in_grid=False):
        return self.select_idx


class FakeDb:
    def __init__(self, nodes=None):
        self._nodes = nodes if nodes is not None else [
            {"id": 1, "hostname": "n1", "server_name": "NODE-1",
             "ip_address": "10.0.0.1", "claster": False},
            {"id": 2, "hostname": "n2", "server_name": "NODE-2",
             "ip_address": "10.0.0.2", "claster": False},
            {"id": 3, "hostname": "c1", "server_name": "cluster1",
             "ip_address": "10.0.0.9", "claster": True},
        ]

    async def get_online_nodes(self):
        return self._nodes


class FakeSsh:
    def __init__(self, upload_ok=True, run_ok=True):
        self.uploads = []
        self.runs = []
        self._upload_ok = upload_ok
        self._run_ok = run_ok

    async def upload(self, host, local, remote, user=None, mode=None):
        self.uploads.append((host, local, remote, user))
        return self._upload_ok

    async def run_stream(self, host, cmd, timeout=300, echo=None, user=None):
        self.runs.append((host, cmd, user))
        return CmdResult(self._run_ok, 0 if self._run_ok else 3, "", "")


@pytest.fixture
def scaffold(monkeypatch):
    """Скрипты ВЕНДОРЕНЫ в assets/fleet_scripts (реальные файлы репо); подменяем
    только локальный запуск (bash не гоняем)."""
    calls = []
    async def fake_local(cmd, cwd):
        calls.append(cmd)
        return 0
    monkeypatch.setattr(scripts, "_run_local", fake_local)
    return calls


def _run(key, db, ssh, ui_backend, dry_run=False):
    audit_rec = {}
    import core.scripts as s
    s.audit.write = lambda rec: audit_rec.update(rec)   # перехват audit
    ui.set_backend(ui_backend)
    try:
        asyncio.run(scripts.run_script(key, db, ssh, dry_run=dry_run))
    finally:
        ui.set_backend(None)
    return audit_rec


# ── local scope: apply-гейт ──
def test_local_apply_confirm_runs_apply(scaffold):
    calls = scaffold
    # whitelist_ip: IP + порты(default), confirm apply=True
    rec = _run("whitelist_ip", FakeDb(), FakeSsh(),
               FakeUi(asks=["1.2.3.4", ""], confirms=[True]))
    assert len(calls) == 2                                   # dry-run + apply
    assert "--apply" not in calls[0]                         # первый прогон — без apply
    assert "--apply" in calls[1]                             # второй — apply
    assert "1.2.3.4" in calls[0] and "--ports" in calls[0]   # позиц. IP + флаг портов
    assert rec["script"] == "whitelist_ip" and rec["scope"] == "local"


def test_local_apply_decline_dry_only(scaffold):
    calls = scaffold
    rec = _run("whitelist_ip", FakeDb(), FakeSsh(),
               FakeUi(asks=["1.2.3.4", ""], confirms=[False]))
    assert len(calls) == 1                                   # только dry-run, apply отклонён
    assert rec["dry_run"] is False


def test_local_readonly_runs_once(scaffold):
    calls = scaffold
    _run("audit_cluster", FakeDb(), FakeSsh(), FakeUi())
    assert len(calls) == 1                                   # read-only: один прогон, без гейта


def test_top_dry_run_no_exec(scaffold):
    calls = scaffold
    rec = _run("audit_cluster", FakeDb(), FakeSsh(), FakeUi(), dry_run=True)
    assert calls == []                                       # dry_run сверху — не исполняем
    assert rec["dry_run"] is True


# ── валидация / отмена ──
def test_invalid_ip_aborts(scaffold):
    calls = scaffold
    _run("whitelist_ip", FakeDb(), FakeSsh(), FakeUi(asks=["не-ip", ""]))
    assert calls == []                                       # плохой IP → стоп до запуска


def test_cancel_on_arg_aborts(scaffold):
    calls = scaffold
    ssh = FakeSsh()
    _run("whitelist_ip", FakeDb(), ssh, FakeUi(asks=[None]))
    assert calls == [] and ssh.uploads == []                # None из ask = отмена


# ── node scope ──
def test_node_pick_one(scaffold):
    ssh = FakeSsh()
    # pw_sweep: пикер клиентов [NODE-1, NODE-2, «Все(2)»]; select_idx=0 → NODE-1
    rec = _run("pw_sweep", FakeDb(), ssh, FakeUi(confirms=[True], select_idx=0))
    assert len(ssh.uploads) == 1 and ssh.uploads[0][0] == "10.0.0.1"
    assert len(ssh.runs) == 1 and ssh.runs[0][2] == (config.PRIV_USER or "root")
    assert rec["scope"] == "node"


def test_node_pick_all_clients(scaffold):
    ssh = FakeSsh()
    # select_idx=2 = «Все(2)» (после 2 клиентов) → оба клиента, cluster-нода исключена
    _run("pw_sweep", FakeDb(), ssh, FakeUi(confirms=[True], select_idx=2))
    assert {u[0] for u in ssh.uploads} == {"10.0.0.1", "10.0.0.2"}


def test_node_decline_confirm_no_run(scaffold):
    ssh = FakeSsh()
    _run("pw_sweep", FakeDb(), ssh, FakeUi(confirms=[False], select_idx=0))
    assert ssh.uploads == [] and ssh.runs == []             # не подтвердил запуск


# ── сбор argv напрямую (позиц./значение/булев) ──
def test_collect_args_bool_and_valued(scaffold):
    # swap_node_ip: OLD, NEW (валид. IP), --reload (bool=True)
    ui.set_backend(FakeUi(asks=["1.1.1.1", "2.2.2.2"], confirms=[True]))
    try:
        argv = asyncio.run(scripts._collect_args(scripts.get_script("swap_node_ip")))
    finally:
        ui.set_backend(None)
    assert argv == ["--old", "1.1.1.1", "--new", "2.2.2.2", "--reload"]


# ── самодостаточность: вендоринг + генерация _nodes.sh ──
def test_all_scripts_bundled():
    import os
    for spec in scripts.SCRIPTS:                            # каждый скрипт лежит в репо DM
        assert os.path.isfile(os.path.join(scripts.BUNDLED_DIR, spec["file"])), spec["file"]


def test_gen_nodes_sh_valid_bash(tmp_path):
    import subprocess
    text = scripts._gen_nodes_sh(FakeDb()._nodes)           # cluster1 + 2 клиента
    assert "CLUSTER_IPS=('10.0.0.9')" in text or "CLUSTER_IPS=(10.0.0.9)" in text
    assert "10.0.0.1" in text and "10.0.0.2" in text        # клиенты
    p = tmp_path / "_nodes.sh"
    p.write_text(text)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr                      # синтаксически валиден
