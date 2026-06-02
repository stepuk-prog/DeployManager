"""Раскатка на ноду: rsync кода (под vova) + установка service-файлов (sudo) + запись VERSION."""
import asyncio
import base64
import os
import shlex

from classes.ssh_client import SshClient
from logs import get_logger
from settings import config

logger = get_logger(__name__)


class Deployer:
    def __init__(self, ssh: SshClient):
        self.ssh = ssh

    @property
    def _ssh_cmd(self) -> str:
        """ssh-транспорт для rsync: ключ/порт/таймаут коннекта + keepalive (рвём зависшие каналы)."""
        return (f"ssh -i {config.SSH_KEY} -p {config.SSH_PORT} "
                f"-o StrictHostKeyChecking=accept-new -o ConnectTimeout={config.SSH_CONNECT_TIMEOUT} "
                f"-o ServerAliveInterval=15 -o ServerAliveCountMax=4")

    @staticmethod
    async def _run_rsync(cmd: list[str], host: str, label: str) -> tuple[bool, str]:
        """Запуск rsync с жёстким таймаутом (не виснем на stalled-передаче). → (ok, stdout)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=config.PROVISION_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("%s %s TIMEOUT (> %ss) — прерван", label, host, config.PROVISION_TIMEOUT)
            return False, ""
        if proc.returncode != 0:
            logger.error("%s %s FAILED (%s): %s", label, host, proc.returncode,
                         (err or b"").decode(errors="replace").strip())
            return False, ""
        return True, (out or b"").decode(errors="replace")

    async def rsync_project(self, host: str, project_dir: str, remote_folder: str,
                            dry_run: bool = False) -> bool:
        """rsync содержимого project_dir → vova@host:remote_folder (под vova, верный владелец).
        dry_run=True — предпросмотр (rsync -n -i, без изменений)."""
        folder = remote_folder.rstrip("/")
        if not dry_run:  # создаём каталог явно (совместимо со старым rsync без --mkpath)
            mk = await self.ssh.run(host, f"mkdir -p {shlex.quote(folder)}", timeout=15)
            if not mk.ok:
                logger.error("mkdir %s FAILED: %s", host, mk.stderr or mk.stdout)
                return False
        src = project_dir.rstrip("/") + "/"
        dst = f"{config.SSH_USER}@{host}:{folder}/"
        cmd = ["rsync", "-az", f"--timeout={config.RSYNC_TIMEOUT}"]
        if dry_run:
            cmd += ["-n", "-i"]   # itemize: показать, что изменилось бы
        if config.RSYNC_DELETE:
            cmd.append("--delete")
        cmd += ["-e", self._ssh_cmd]
        for inc in config.RSYNC_INCLUDES:       # include ПЕРЕД exclude (первое правило выигрывает)
            cmd.append(f"--include={inc}")
        for ex in config.RSYNC_EXCLUDES:
            cmd.append(f"--exclude={ex}")
        cmd += [src, dst]
        logger.info("rsync%s → %s:%s", " (dry-run)" if dry_run else "", host, folder)
        ok, out = await self._run_rsync(cmd, host, "rsync")
        if not ok:
            return False
        if dry_run:
            changes = out.strip()
            print(f"  [{host}] изменения rsync:\n" +
                  ("\n".join("      " + l for l in changes.splitlines()) if changes else "      (нет)"))
        return True

    async def sync_env(self, host: str, project_dir: str, remote_folder: str,
                       dry_run: bool = False) -> bool:
        """Залить ТОЛЬКО локальный .env → remote_folder/.env (обновление настроек без передеплоя)."""
        src = os.path.join(project_dir.rstrip("/"), ".env")
        if not os.path.isfile(src):
            logger.error(".env не найден локально: %s", src)
            return False
        folder = remote_folder.rstrip("/")
        if not dry_run:
            mk = await self.ssh.run(host, f"mkdir -p {shlex.quote(folder)}", timeout=15)
            if not mk.ok:
                logger.error("mkdir %s FAILED: %s", host, mk.stderr or mk.stdout)
                return False
        dst = f"{config.SSH_USER}@{host}:{folder}/.env"
        cmd = (["rsync", "-az", f"--timeout={config.RSYNC_TIMEOUT}"]
               + (["-n", "-i"] if dry_run else []) + ["-e", self._ssh_cmd, src, dst])
        logger.info("sync .env%s → %s:%s/.env", " (dry-run)" if dry_run else "", host, folder)
        ok, _out = await self._run_rsync(cmd, host, "sync_env")
        return ok

    async def sync_units(self, host: str, project_dir: str, remote_folder: str,
                         service_files: list[str], dry_run: bool = False) -> bool:
        """Обновить service-файлы: rsync локальных systemd/* → remote_folder/systemd,
        затем install_services (cp в /etc/systemd/system + daemon-reload под root)."""
        if not service_files:
            return True
        folder = remote_folder.rstrip("/")
        local_sd = os.path.join(project_dir.rstrip("/"), "systemd")
        if not os.path.isdir(local_sd):
            logger.error("нет локальной папки systemd/: %s", local_sd)
            return False
        if not dry_run:
            mk = await self.ssh.run(host, f"mkdir -p {shlex.quote(folder + '/systemd')}", timeout=15)
            if not mk.ok:
                logger.error("mkdir %s FAILED: %s", host, mk.stderr or mk.stdout)
                return False
        dst = f"{config.SSH_USER}@{host}:{folder}/systemd/"
        cmd = (["rsync", "-az", f"--timeout={config.RSYNC_TIMEOUT}"] + (["-n", "-i"] if dry_run else [])
               + ["-e", self._ssh_cmd, local_sd.rstrip("/") + "/", dst])
        logger.info("sync юниты%s → %s:%s/systemd", " (dry-run)" if dry_run else "", host, folder)
        ok, _out = await self._run_rsync(cmd, host, "sync_units")
        if not ok:
            return False
        if dry_run:
            return True
        return await self.install_services(host, remote_folder, service_files)  # cp в /etc + daemon-reload

    async def install_services(self, host: str, remote_folder: str, service_files: list[str]) -> bool:
        """sudo cp юнитов из remote_folder/systemd в /etc/systemd/system + daemon-reload."""
        if not service_files:
            return True
        cps = []
        for name in service_files:
            src = shlex.quote(os.path.join(remote_folder, "systemd", name))
            dst = shlex.quote(os.path.join(config.SYSTEMD_DIR, name))
            cps.append(f"cp {src} {dst}")
        inner = " && ".join(cps + ["systemctl daemon-reload"])
        res = await self.ssh.run_priv(host, f"sh -c {shlex.quote(inner)}", timeout=30)
        if not res.ok:
            logger.error("install_services %s FAILED: %s", host, res.stderr or res.stdout)
        return res.ok

    async def provision(self, host: str, remote_folder: str, extra_cmds: list[str]) -> bool:
        """Окружение на ноде: venv → pip install -U pip → pip install -r requirements.txt
        → доп. установки (extra_cmds, напр. 'playwright install firefox'). Команды из README — в коде."""
        folder = shlex.quote(remote_folder.rstrip("/"))
        venv = config.VENV_DIR
        steps = [
            f"cd {folder}",
            f"{{ test -d {venv} || {config.PYTHON_BIN} -m venv {venv}; }}",
            f"{venv}/bin/pip install -q -U pip",
            f"{venv}/bin/pip install -q -r requirements.txt",
        ]
        steps += [f"{venv}/bin/{c}" for c in extra_cmds]   # extra_cmds — без префикса venv/bin
        cmd = " && ".join(steps)
        logger.info("provision → %s", host)
        res = await self.ssh.run(host, f"bash -lc {shlex.quote(cmd)}", timeout=config.PROVISION_TIMEOUT)
        if not res.ok:
            logger.error("provision %s FAILED: %s", host, (res.stderr or res.stdout)[-500:])
        return res.ok

    async def write_version(self, host: str, remote_folder: str, manifest_json: str) -> bool:
        """Записать VERSION-манифест в remote_folder (под vova, own dir)."""
        b64 = base64.b64encode(manifest_json.encode("utf-8")).decode("ascii")
        dst = shlex.quote(os.path.join(remote_folder, config.VERSION_FILE))
        cmd = f"sh -c {shlex.quote(f'echo {b64} | base64 -d > {dst}')}"
        res = await self.ssh.run(host, cmd, timeout=15)
        if not res.ok:
            logger.error("write_version %s FAILED: %s", host, res.stderr or res.stdout)
        return res.ok
