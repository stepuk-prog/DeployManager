"""Оркестрация деплоя на выбранные ноды: rsync кода → установка юнитов → запись VERSION."""
import asyncio
from dataclasses import dataclass

from classes.deployer import Deployer
from classes.manifest import LocalVersion, build_manifest
from classes.ssh_client import SshClient
from core import provision, ui
from core.verify import local_hashes, remote_hashes
from settings import config


@dataclass
class DeployResult:
    node: str
    ip: str
    ok: bool
    step: str            # на каком шаге остановились / 'done'
    detail: str = ""


def node_flags(step: str) -> tuple[bool, bool]:
    """Из шага DeployResult → (folder_deployed, service_installed) для журнала.
    folder доставлена, если прошли дальше rsync; сервис установлен на write_version/done."""
    return step not in ("ping", "rsync"), step in ("write_version", "done")


async def _deps_changed(ssh: SshClient, host: str, remote_folder: str, project_dir: str) -> bool:
    """Менялся ли requirements.txt относительно ноды (для отчёта об обновлении пакетов).
    Сверяем ДО rsync: хэш на ноде != локальному → зависимости изменились (на новой ноде
    файла ещё нет → тоже True). Нет локального requirements.txt → False (нечего ставить)."""
    lh = local_hashes(project_dir, ["requirements.txt"]).get("requirements.txt")
    if lh is None:
        return False
    rh = (await remote_hashes(ssh, host, remote_folder, ["requirements.txt"])).get("requirements.txt")
    return rh != lh


async def _deploy_one(ssh: SshClient, deployer: Deployer, node, project_dir: str,
                      remote_folder: str, service_files: list[str], manifest_json: str,
                      extra_cmds: list[str], dry_run: bool) -> DeployResult:
    ip = node["ip_address"]
    name = node["server_name"] or node["hostname"]
    if not await ssh.ping(ip):
        return DeployResult(name, ip, False, "ping", "нет SSH")
    if dry_run:  # только предпросмотр rsync, без изменений
        ok = await deployer.rsync_project(ip, project_dir, remote_folder, dry_run=True)
        return DeployResult(name, ip, ok, "dry-run")
    deps_changed = await _deps_changed(ssh, ip, remote_folder, project_dir)  # до rsync — для отчёта
    if not await deployer.rsync_project(ip, project_dir, remote_folder):
        return DeployResult(name, ip, False, "rsync")
    if config.PROVISION:
        if not await deployer.provision(ip, remote_folder, extra_cmds):
            return DeployResult(name, ip, False, "provision")
        print(f"  [{name}] пакеты: pip install -r выполнен ✅"
              + ("  (зависимости изменились)" if deps_changed else ""))
    elif deps_changed:
        print(f"  [{name}] ⚠️ зависимости изменились, но PROVISION=0 — пакеты НЕ обновлены")
    if not await deployer.install_services(ip, remote_folder, service_files):
        return DeployResult(name, ip, False, "install_services")
    if provision.is_browser_project(project_dir):   # браузер-боты: pw_lock_sweep drop-in на юниты
        if not await deployer.install_pw_sweep_dropins(ip, service_files):
            return DeployResult(name, ip, False, "pw_sweep_dropins")
    if not await deployer.write_version(ip, remote_folder, manifest_json):
        return DeployResult(name, ip, False, "write_version")
    return DeployResult(name, ip, True, "done")


async def deploy(ssh: SshClient, deployer: Deployer, nodes: list, project_dir: str,
                 remote_folder: str, service_files: list[str], local: LocalVersion,
                 deployed_by: str, deployed_at: str, extra_cmds: list[str] | None = None,
                 dry_run: bool = False) -> list[DeployResult]:
    """Деплой на все ноды параллельно. service_files — имена юнитов для установки в /etc;
    extra_cmds — доп. установки в venv (напр. playwright install firefox); dry_run — предпросмотр."""
    manifest_json = build_manifest(local, deployed_by, deployed_at)
    extra_cmds = extra_cmds or []
    total = len(nodes)
    done = {"n": 0}
    verb = "Предпросмотр" if dry_run else "Деплой"
    ui.progress(f"{verb}: 0/{total} нод…")

    async def _one(n):
        r = await _deploy_one(ssh, deployer, n, project_dir, remote_folder, service_files,
                              manifest_json, extra_cmds, dry_run)
        done["n"] += 1                         # одиночный поток asyncio → инкремент атомарен
        ui.progress(f"{verb}: {done['n']}/{total} нод…")
        return r

    results = await asyncio.gather(*[_one(n) for n in nodes])   # порядок сохраняется
    return list(results)


def print_deploy_results(results: list[DeployResult]) -> None:
    print(f"\n{'НОДА':18} {'IP':16} РЕЗУЛЬТАТ")
    print("-" * 60)
    for r in results:
        status = "✅ done" if r.ok else f"⛔ {r.step} {r.detail}"
        print(f"{r.node:18} {r.ip:16} {status}")
