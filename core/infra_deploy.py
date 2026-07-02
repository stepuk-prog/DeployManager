"""Деплой инфра-компонентов диспетчера (GD/WD/CD/DispatcherCtl) — В ОБХОД programdata.

Control-plane компоненты (GlobalDispatcher2, Watchdog2, CronDispatcher2, DispatcherCtl)
НЕ зарегистрированы в program.programdata и НЕ привязаны к service_status — это не
бот-программы, а инфраструктура. Обычный флоу DeployManager им не подходит.

Наборы нод фиксированы (guard):
  • GD / CD / DispatcherCtl → ТОЛЬКО кластерные ноды (vocabulary.nodes.claster=true);
  • WD → все online-ноды.

common доставляется РАЗДЕЛЯЕМО: один /opt/common на ноду, каждый компонент-venv ставит
его editable (`pip install -e /opt/common`) — deps тянутся из common/setup.py install_requires
(поэтому requirements.txt самих GD/WD пустые). Импортируемость `common` даёт PYTHONPATH=/opt
(прописан в юнитах). См. также CLAUDE.md.

── .env ──
Реальный .env КАЖДОГО компонента живёт только на нодах, в git его нет (секрет-гейт).
Он раскладывается на «единый секрет-блок компонента» (PG_*, LOG_TELEGRAM_TOKEN, общие
chat/thread ID — ОДИНАКОВ для всех нод компонента) + «идентичность ноды» (NODE_ID/IP/NAME —
выводима из vocabulary.nodes). Поэтому:
  • базовый секрет-блок лежит В DeployManager: env/<KEY>.env (gitignored, оператор ведёт вручную);
  • на деплое DeployManager РЕНДЕРИТ финальный .env = база + строки идентичности из БД и
    пишет его на ноду (chmod 600). fresh-нода получает верный .env без ручной правки.
CD/DispatcherCtl полностью единые (node_env пустой). Нет env/<KEY>.env → .env НЕ пишем
(прод сохраняется), только предупреждаем.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import os
import shlex
from dataclasses import dataclass
from datetime import datetime

from classes.deployer import Deployer
from classes.manifest import build_manifest, local_version
from classes.ssh_client import SshClient
from core import status, ui
from core.deploy import DeployResult, print_deploy_results
from database.db import Database
from settings import config

COMMON_SUBDIR = "Dispatcher2.0/common"
COMMON_REMOTE = "/opt/common"
ENV_BASE_DIR = os.path.join(config.ROOT, "env")   # gitignored: env/<KEY>.env


@dataclass(frozen=True)
class InfraComponent:
    key: str
    label: str
    project_subdir: str                    # относительно config.PROJECTS_DIR
    remote_folder: str                     # /opt/...
    units: tuple[tuple[str, str], ...]     # (unit_name, src_relpath_на_ноде)
    nodes: str                             # "all" | "cluster"
    node_env: dict                         # env_key -> колонка vocabulary.nodes (идентичность ноды)
    needs_common: bool = True
    restart: bool = True

    @property
    def env_base(self) -> str:
        return os.path.join(ENV_BASE_DIR, f"{self.key}.env")


# src_relpath юнита — путь ВНУТРИ remote_folder после rsync (структура сохраняется):
# юниты у всех компонентов в systemd/ (GD/WD/CD). DispatcherCtl — CLI без службы.
INFRA_COMPONENTS: dict[str, InfraComponent] = {
    "GD": InfraComponent(
        key="GD", label="GlobalDispatcher2",
        project_subdir="Dispatcher2.0/GlobalDispatcher2",
        remote_folder="/opt/GlobalDispatcher2",
        units=(("dispatcher.service", "systemd/dispatcher.service"),
               ("gd-alert.service", "systemd/gd-alert.service")),
        nodes="cluster",
        # GD_NODE_HOSTNAME — display-имя (не системный hostname): на нодах это server_name.
        node_env={"GD_NODE_ID": "id", "GD_NODE_HOSTNAME": "server_name"},
    ),
    "WD": InfraComponent(
        key="WD", label="Watchdog2",
        project_subdir="Dispatcher2.0/Watchdog2",
        remote_folder="/opt/Watchdog2",
        units=(("watchdog.service", "systemd/watchdog.service"),
               ("watchdog-alert.service", "systemd/watchdog-alert.service")),
        nodes="all",
        # WATCHDOG_NODE_NAME — display-имя (заголовок форум-темы + префикс логов) = server_name.
        node_env={"WATCHDOG_NODE_ID": "id", "WATCHDOG_NODE_IP": "ip_address",
                  "WATCHDOG_NODE_NAME": "server_name"},
    ),
    "CD": InfraComponent(
        key="CD", label="CronDispatcher2",
        project_subdir="Dispatcher2.0/CronDispatcher2",
        remote_folder="/opt/cron_disp2",
        units=(("cron-dispatcher.service", "systemd/cron-dispatcher.service"),
               ("cd-alert.service", "systemd/cd-alert.service")),
        nodes="cluster",
        node_env={},
    ),
    "DispatcherCtl": InfraComponent(
        key="DispatcherCtl", label="DispatcherCtl (CLI-оператор)",
        project_subdir="Dispatcher2.0/DispatcherCtl",
        remote_folder="/opt/DispatcherCtl",
        units=(),
        nodes="cluster",
        node_env={},
        restart=False,
    ),
}

# Операции меню (паритет со стандартными ветками DeployManager).
# Лейблы — КОРОТКИЕ (кнопка); подробности — в _OP_DETAILS (печатаются после выбора
# и в диалоге подтверждения), не засоряют меню.
_OPERATIONS = ("new", "add", "sync-env", "restart", "check", "manage", "dry-run", "uninstall")
_OP_LABELS = (
    "🚀 Деплой с нуля",
    "➕ Добавить ноду",
    "♻️ Sync .env + юниты",
    "🔄 Перезапуск службы",
    "🔍 Сверка версий",
    "🎛️ Управление службой",
    "👀 Предпросмотр (dry-run)",
    "🗑️ Деинсталляция",
)
_OP_DETAILS = {
    "new": "код+common+venv+юниты+.env+restart — на ВСЕ целевые ноды",
    "add": "то же, что «с нуля», но только на ноды без компонента",
    "sync-env": "пере-рендер .env из базы+БД + обновить юниты + restart (без rsync кода)",
    "restart": "systemctl restart на всех целевых нодах",
    "check": "VERSION-манифест на нодах vs локальный git (read-only)",
    "manage": "start / stop / restart службы",
    "dry-run": "rsync --dry-run — что изменилось бы, без изменений",
    "uninstall": "stop+disable + удалить юниты и папку компонента",
}


def _node_name(node) -> str:
    return node["server_name"] or node["hostname"]


def _resolve_targets(comp: InfraComponent, nodes: list) -> tuple[list, list]:
    """(targets, skipped) с учётом guard: cluster-only компонент — только claster=true."""
    if comp.nodes == "cluster":
        return [n for n in nodes if n["claster"]], [n for n in nodes if not n["claster"]]
    return list(nodes), []


# ── .env рендер ──

def _read_env_base(comp: InfraComponent) -> str | None:
    """Единый секрет-блок компонента из env/<KEY>.env. None — файла нет (тогда .env не пишем)."""
    try:
        with open(comp.env_base, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _render_env(base_text: str, node, node_env: dict) -> str:
    """База + строки идентичности из vocabulary.nodes. Ключи из node_env в базе замещаются
    (не дублируются) — берём значение из БД как источник истины."""
    managed = set(node_env)
    out: list[str] = []
    for line in base_text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() in managed:
            continue
        out.append(line)
    if node_env:
        out += ["", "# --- идентичность ноды (авто-рендер DeployManager из vocabulary.nodes) ---"]
        out += [f"{k}={node[col]}" for k, col in node_env.items()]
    return "\n".join(out).rstrip("\n") + "\n"


async def _write_env(ssh: SshClient, host: str, remote_folder: str, text: str) -> bool:
    """Записать отрендеренный .env → remote_folder/.env под vova, chmod 600 (секреты)."""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    dst = shlex.quote(os.path.join(remote_folder, ".env"))
    inner = f"echo {b64} | base64 -d > {dst} && chmod 600 {dst}"
    res = await ssh.run(host, f"sh -c {shlex.quote(inner)}", timeout=15)
    return res.ok


# ── systemd на ноде (root) ──

async def _install_units(ssh: SshClient, host: str, comp: InfraComponent) -> bool:
    """cp юнитов remote_folder/<src> → /etc/systemd/system + daemon-reload + enable (root)."""
    if not comp.units:
        return True
    cps, names = [], []
    for unit_name, src_rel in comp.units:
        src = shlex.quote(os.path.join(comp.remote_folder, src_rel))
        dst = shlex.quote(os.path.join(config.SYSTEMD_DIR, unit_name))
        cps.append(f"cp {src} {dst}")
        names.append(shlex.quote(unit_name))
    inner = " && ".join(cps + ["systemctl daemon-reload", f"systemctl enable {' '.join(names)}"])
    res = await ssh.run_priv(host, f"sh -c {shlex.quote(inner)}", timeout=30)
    return res.ok


async def _systemctl(ssh: SshClient, host: str, comp: InfraComponent, cmd: str) -> bool:
    """systemctl {cmd} для всех юнитов компонента (root). restart стартует и остановленный."""
    names = " ".join(shlex.quote(u) for u, _ in comp.units)
    res = await ssh.run_priv(host, f"systemctl {cmd} {names}", timeout=60)
    return res.ok


async def _uninstall_one(ssh: SshClient, comp: InfraComponent, node) -> DeployResult:
    """stop+disable+удалить юниты (root) и папку компонента (vova). /opt/common НЕ трогаем (общий)."""
    ip = node["ip_address"]
    name = _node_name(node)
    if comp.units:
        names = " ".join(shlex.quote(u) for u, _ in comp.units)
        etc = " ".join(shlex.quote(os.path.join(config.SYSTEMD_DIR, u)) for u, _ in comp.units)
        inner = (f"systemctl disable --now {names} 2>/dev/null; "
                 f"rm -f {etc}; systemctl daemon-reload")
        res = await ssh.run_priv(ip, f"sh -c {shlex.quote(inner)}", timeout=60)
        if not res.ok:
            return DeployResult(name, ip, False, "units", res.stderr or res.stdout)
    rm = await ssh.run(ip, f"rm -rf {shlex.quote(comp.remote_folder)}", timeout=30)
    if not rm.ok:
        return DeployResult(name, ip, False, "rm-folder", rm.stderr or rm.stdout)
    return DeployResult(name, ip, True, "uninstalled")


# ── деплой одной ноды ──

async def _deploy_one(ssh: SshClient, deployer: Deployer, comp: InfraComponent,
                      project_dir: str, common_dir: str, node, manifest_json: str,
                      *, rsync_code: bool, write_env: bool, dry_run: bool) -> DeployResult:
    ip = node["ip_address"]
    name = _node_name(node)
    if comp.nodes == "cluster" and not node["claster"]:
        return DeployResult(name, ip, False, "guard", "не кластерная нода (claster=false)")
    if not await ssh.ping(ip):
        return DeployResult(name, ip, False, "ping", "нет SSH")
    if dry_run:
        ok = await deployer.rsync_project(ip, project_dir, comp.remote_folder,
                                          dry_run=True, extra_excludes=[".env"])
        return DeployResult(name, ip, ok, "dry-run")

    if rsync_code:
        if comp.needs_common and not await deployer.rsync_project(
                ip, common_dir, COMMON_REMOTE, extra_excludes=[".env"]):
            return DeployResult(name, ip, False, "rsync-common")
        if not await deployer.rsync_project(ip, project_dir, comp.remote_folder,
                                            extra_excludes=[".env"]):
            return DeployResult(name, ip, False, "rsync")

    if write_env:
        base = _read_env_base(comp)
        if base is None:
            print(f"  [{name}] ⚠️ нет базы {comp.env_base} — .env пропущен (прод сохранён)")
        elif not await _write_env(ssh, ip, comp.remote_folder, _render_env(base, node, comp.node_env)):
            return DeployResult(name, ip, False, "write_env")

    if rsync_code and config.PROVISION:
        extra = [f"pip install -q -e {COMMON_REMOTE}"] if comp.needs_common else []
        if not await deployer.provision(ip, comp.remote_folder, extra):
            return DeployResult(name, ip, False, "provision")

    if not await _install_units(ssh, ip, comp):
        return DeployResult(name, ip, False, "install_units")

    if rsync_code and not await deployer.write_version(ip, comp.remote_folder, manifest_json):
        return DeployResult(name, ip, False, "write_version")

    if comp.restart and comp.units and not await _systemctl(ssh, ip, comp, "restart"):
        return DeployResult(name, ip, True, "done", "⚠️ установлен, но restart не удался")
    return DeployResult(name, ip, True, "done")


# ── управление / деинсталляция (отдельные ветки) ──

async def _run_manage(ssh: SshClient, comp: InfraComponent, targets: list,
                      cmd: str | None = None) -> None:
    """start/stop/restart юнитов на нодах. cmd=None → спросить; иначе прямое действие
    (напр. кнопка «Перезапустить» после деплоя)."""
    if not comp.units:
        print(f"{comp.label} — служб нет (CLI), управление неприменимо.")
        return
    if cmd is None:
        idx = await ui.select("Команда службе:", ["▶ start", "■ stop", "♻ restart"])
        if idx is None:
            return
        cmd = ("start", "stop", "restart")[idx]
    if not await ui.confirm(f"systemctl {cmd} {comp.label} на {len(targets)} нод(ах)?",
                            danger=(cmd != "start")):
        print("Отменено.")
        return

    async def _one(n):
        ip, name = n["ip_address"], _node_name(n)
        if comp.nodes == "cluster" and not n["claster"]:
            return DeployResult(name, ip, False, "guard", "не кластер")
        if not await ssh.ping(ip):
            return DeployResult(name, ip, False, "ping", "нет SSH")
        return DeployResult(name, ip, await _systemctl(ssh, ip, comp, cmd), cmd)

    print_deploy_results(list(await asyncio.gather(*[_one(n) for n in targets])))


async def _run_uninstall(ssh: SshClient, comp: InfraComponent, targets: list) -> None:
    if not await ui.confirm(
            f"ДЕИНСТАЛЛЯЦИЯ {comp.label} с {len(targets)} нод(ы): stop+disable+удалить "
            f"юниты и {comp.remote_folder}. /opt/common НЕ трогаем. Продолжить?", danger=True):
        print("Отменено.")
        return

    async def _one(n):
        ip, name = n["ip_address"], _node_name(n)
        if comp.nodes == "cluster" and not n["claster"]:
            return DeployResult(name, ip, False, "guard", "не кластер")
        if not await ssh.ping(ip):
            return DeployResult(name, ip, False, "ping", "нет SSH")
        return await _uninstall_one(ssh, comp, n)

    print_deploy_results(list(await asyncio.gather(*[_one(n) for n in targets])))


# ── точка входа ──

async def run_infra(db: Database, ssh: SshClient, *, component: str | None = None,
                    operation: str | None = None) -> None:
    """Инфра-флоу одного control-plane компонента. operation — из аргумента
    (CLI-флаги) либо None → меню (как в GUI по кнопке). Значения: см. _OPERATIONS."""
    # 1) компонент
    if component and component in INFRA_COMPONENTS:
        comp = INFRA_COMPONENTS[component]
    else:
        keys = list(INFRA_COMPONENTS)
        labels = [
            f"{INFRA_COMPONENTS[k].label} → {INFRA_COMPONENTS[k].remote_folder} "
            f"[{'все ноды' if INFRA_COMPONENTS[k].nodes == 'all' else 'кластер'}]"
            for k in keys
        ]
        idx = await ui.select("Инфра-компонент:", labels)
        if idx is None:
            return
        comp = INFRA_COMPONENTS[keys[idx]]

    # 2) операция (выпадающее меню — как ветки обычных программ)
    if operation is None:
        idx = await ui.select(f"{comp.label}: действие?", list(_OP_LABELS))
        if idx is None:
            return
        operation = _OPERATIONS[idx]
    if operation not in _OPERATIONS:
        print(f"🛑 Неизвестное действие: {operation!r}")
        return

    # 3) целевые ноды + guard
    nodes = await db.get_online_nodes()
    if not nodes:
        print("🛑 Нет online-нод в vocabulary.nodes.")
        return
    targets, skipped = _resolve_targets(comp, nodes)
    _verb = {"new": "Деплой с нуля", "add": "Добавить ноду", "sync-env": "Sync .env+юниты",
             "restart": "Перезапуск", "check": "Сверка версий", "manage": "Управление",
             "dry-run": "Предпросмотр", "uninstall": "Деинсталляция"}[operation]
    print(f"\n🌐 {_verb}: {comp.label}  "
          f"[ноды: {'все online' if comp.nodes == 'all' else 'только кластер (claster=true)'}]")
    print(f"   → {_OP_DETAILS[operation]}")
    if skipped:
        print(f"   ⏭️  пропускаю не-кластерные ({len(skipped)}): "
              + ", ".join(_node_name(n) for n in skipped))
    if not targets:
        print("🛑 Нет подходящих нод для этого компонента.")
        return
    print(f"   цель: {len(targets)} нод — " + ", ".join(_node_name(n) for n in targets))

    # 4) ветки без локального кода
    if operation == "manage":
        await _run_manage(ssh, comp, targets)
        return
    if operation == "restart":
        await _run_manage(ssh, comp, targets, cmd="restart")
        return
    if operation == "uninstall":
        await _run_uninstall(ssh, comp, targets)
        return

    # 5) ветки, работающие с локальным кодом
    project_dir = os.path.join(config.PROJECTS_DIR, comp.project_subdir)
    common_dir = os.path.join(config.PROJECTS_DIR, COMMON_SUBDIR)
    if not os.path.isdir(project_dir):
        print(f"🛑 Нет каталога проекта: {project_dir}")
        return
    if comp.needs_common and not os.path.isfile(os.path.join(common_dir, "setup.py")):
        print(f"🛑 Нет common/setup.py: {common_dir} — editable-установка невозможна.")
        return
    local = local_version(project_dir)
    print(f"   версия: v{local.short} ({local.branch}){'  ⚠️ DIRTY' if local.dirty else ''}")

    # сверка версий на нодах (VERSION vs git) — programdata не нужна
    statuses = await status.check_status(ssh, targets, comp.remote_folder, local)
    status.print_status(local, statuses)
    if operation == "check":
        return

    dry_run = operation == "dry-run"
    rsync_code = operation in ("new", "add", "dry-run")
    write_env = operation in ("new", "add", "sync-env")

    # add → только ноды без компонента (нет VERSION)
    if operation == "add":
        missing = {s.node for s in statuses if s.state == "missing"}
        targets = [n for n in targets if _node_name(n) in missing]
        if not targets:
            print("Все целевые ноды уже имеют компонент — нечего добавлять.")
            return
        print(f"   добавляю на {len(targets)}: " + ", ".join(_node_name(n) for n in targets))

    if write_env and _read_env_base(comp) is None:
        print(f"   ⚠️ Нет базы {comp.env_base} — .env НЕ будет записан (прод сохранён). "
              f"Создай из env/{comp.key}.env.example.")

    if not dry_run:
        bits = []
        if write_env:
            bits.append(".env")
        if comp.restart and comp.units:
            bits.append("restart")
        note = (" (+ " + ", ".join(bits) + ")") if bits else ""
        if not await ui.confirm(
                f"{_verb} {comp.label} на {len(targets)} нод(ы){note}?\n"
                f"   {_OP_DETAILS[operation]}", danger=True):
            print("Отменено.")
            return

    manifest_json = build_manifest(local, getpass.getuser(),
                                   datetime.now().isoformat(timespec="seconds"))
    deployer = Deployer(ssh)
    verb = "Предпросмотр" if dry_run else "Применяю"
    ui.progress(f"{verb}: 0/{len(targets)} нод…")
    done = {"n": 0}

    async def _one(n):
        r = await _deploy_one(ssh, deployer, comp, project_dir, common_dir, n, manifest_json,
                              rsync_code=rsync_code, write_env=write_env, dry_run=dry_run)
        done["n"] += 1
        ui.progress(f"{verb}: {done['n']}/{len(targets)} нод…")
        return r

    print_deploy_results(list(await asyncio.gather(*[_one(n) for n in targets])))


__all__ = ["INFRA_COMPONENTS", "InfraComponent", "run_infra"]
