"""Оркестрация деплоя на выбранные ноды: rsync кода → установка юнитов → запись VERSION."""
import asyncio
from dataclasses import dataclass

from classes.deployer import Deployer
from classes.manifest import LocalVersion, build_manifest
from classes.ssh_client import SshClient
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
    if not await deployer.rsync_project(ip, project_dir, remote_folder):
        return DeployResult(name, ip, False, "rsync")
    if config.PROVISION and not await deployer.provision(ip, remote_folder, extra_cmds):
        return DeployResult(name, ip, False, "provision")
    if not await deployer.install_services(ip, remote_folder, service_files):
        return DeployResult(name, ip, False, "install_services")
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
    results = await asyncio.gather(*[
        _deploy_one(ssh, deployer, n, project_dir, remote_folder, service_files,
                    manifest_json, extra_cmds, dry_run)
        for n in nodes
    ])
    return list(results)


def print_deploy_results(results: list[DeployResult]) -> None:
    print(f"\n{'НОДА':18} {'IP':16} РЕЗУЛЬТАТ")
    print("-" * 60)
    for r in results:
        status = "✅ done" if r.ok else f"⛔ {r.step} {r.detail}"
        print(f"{r.node:18} {r.ip:16} {status}")
