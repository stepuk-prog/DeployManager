"""SSH-клиент на asyncssh: вход под vova, привилегии через passwordless sudo.

Кэширует одно соединение на хост (меньше хендшейков): connect лениво, переиспользуется
для всех команд ноды (в т.ч. параллельных каналов). При ошибке соединения — один реконнект.
"""
import asyncio
import shlex
from collections import defaultdict
from dataclasses import dataclass

import asyncssh

from logs import get_logger
from settings import config

logger = get_logger(__name__)


@dataclass
class CmdResult:
    ok: bool
    exit_status: int
    stdout: str
    stderr: str


class SshClient:
    def __init__(self):
        # ключ кэша — (user, host): один пользователь может ходить под vova, другой под root
        self._conns: dict[tuple[str, str], asyncssh.SSHClientConnection] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._keys: dict[str, asyncssh.SSHKey] = {}   # кэш загруженных приватных ключей по пути

    def _load_key(self, path: str, passphrase, label: str) -> asyncssh.SSHKey:
        """Прочитать приватный ключ с понятной ошибкой (какой ключ/путь и вероятная причина)."""
        cached = self._keys.get(path)
        if cached is not None:
            return cached
        try:
            key = asyncssh.read_private_key(path, passphrase=passphrase)
        except FileNotFoundError:
            raise RuntimeError(f"файл ключа не найден: {label}={path}") from None
        except asyncssh.KeyImportError as e:
            msg = str(e)
            if "passphrase" in msg.lower():
                raise RuntimeError(
                    f"ключ {label}={path} зашифрован паролем — задай {label}_PASSPHRASE в .env") from None
            raise RuntimeError(
                f"ключ {label}={path} не читается ({msg}). Нужен НЕшифрованный приватный ключ "
                f"в формате OpenSSH/PEM (не .pub, не PuTTY .ppk, не повреждённый)") from None
        self._keys[path] = key
        return key

    async def _get(self, host: str, user: str) -> asyncssh.SSHClientConnection:
        key = (user, host)
        async with self._locks[key]:
            conn = self._conns.get(key)
            if conn is None:
                # отдельный ключ для PRIV_USER, если задан PRIV_KEY (ключ root в другом месте)
                use_priv = config.PRIV_KEY and config.PRIV_USER and user == config.PRIV_USER
                key_file = config.PRIV_KEY if use_priv else config.SSH_KEY
                label = "PRIV_KEY" if use_priv else "SSH_KEY"
                passphrase = config.PRIV_KEY_PASSPHRASE if use_priv else config.SSH_KEY_PASSPHRASE
                client_key = self._load_key(key_file, passphrase, label)
                conn = await asyncssh.connect(
                    host=host, port=config.SSH_PORT, username=user,
                    client_keys=[client_key], known_hosts=None,
                    connect_timeout=config.SSH_CONNECT_TIMEOUT,
                    keepalive_interval=15, keepalive_count_max=4,  # отваливаем зависшие соединения
                )
                self._conns[key] = conn
            return conn

    async def _drop(self, key: tuple[str, str]) -> None:
        conn = self._conns.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except (Exception,):
                pass

    async def run(self, host: str, command: str, timeout: int = 30, sudo: bool = False,
                  user: str | None = None) -> CmdResult:
        """Команда на ноде под user (по умолчанию SSH_USER). sudo=True → префикс `sudo -n`."""
        user = user or config.SSH_USER
        cmd = f"sudo -n {command}" if sudo else command
        last = ""
        for _ in (1, 2):  # одна попытка реконнекта при обрыве соединения
            try:
                conn = await self._get(host, user)
                res = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
                ex = res.exit_status if res.exit_status is not None else 1
                return CmdResult(ex == 0, ex, (res.stdout or "").strip(), (res.stderr or "").strip())
            except asyncio.TimeoutError:
                logger.error("SSH %s: таймаут %ss на %r", host, timeout, command)
                return CmdResult(False, 255, "", f"timeout {timeout}s")
            except (asyncssh.Error, OSError) as e:
                last = str(e)
                await self._drop((user, host))  # на 2-й попытке создастся новое соединение
        logger.error("SSH %s: ошибка %r — %s", host, command, last)
        return CmdResult(False, 255, "", last)

    async def run_priv(self, host: str, command: str, timeout: int = 30) -> CmdResult:
        """Привилегированная команда. PRIV_USER задан (напр. root) → вход под ним без sudo;
        иначе — текущий пользователь + `sudo -n` (нужен passwordless sudo)."""
        if config.PRIV_USER:
            return await self.run(host, command, timeout=timeout, user=config.PRIV_USER)
        return await self.run(host, command, timeout=timeout, sudo=True)

    async def _stream(self, conn, command: str, timeout: int, echo) -> CmdResult:
        """Прогнать команду, стримя вывод (stdout+stderr слиты) построчно в echo(line).
        Для длинных прогонов (apt, сборка haproxy, provision) — живой лог, а не тишина."""
        lines: list[str] = []
        try:
            async with conn.create_process(f"{command} 2>&1", term_type=None) as proc:
                async def _pump():
                    async for line in proc.stdout:
                        line = line.rstrip("\n")
                        lines.append(line)
                        if echo:
                            echo(line)
                await asyncio.wait_for(_pump(), timeout=timeout)
                await proc.wait()
                ex = proc.exit_status if proc.exit_status is not None else 1
        except asyncio.TimeoutError:
            return CmdResult(False, 255, "\n".join(lines), f"timeout {timeout}s")
        return CmdResult(ex == 0, ex, "\n".join(lines), "")

    async def run_stream(self, host: str, command: str, timeout: int = 300,
                         echo=None, user: str | None = None) -> CmdResult:
        """run() со стримингом вывода (echo построчно). Под user (по умолч. SSH_USER)."""
        user = user or config.SSH_USER
        last = ""
        for _ in (1, 2):
            try:
                conn = await self._get(host, user)
                return await self._stream(conn, command, timeout, echo)
            except (asyncssh.Error, OSError) as e:
                last = str(e)
                await self._drop((user, host))
        logger.error("SSH %s: стрим %r — %s", host, command, last)
        return CmdResult(False, 255, "", last)

    async def upload(self, host: str, local_path: str, remote_path: str,
                     user: str | None = None, mode: int | None = None) -> bool:
        """SFTP-заливка файла на ноду под user (по кэш-соединению, ключ). mode — chmod после."""
        user = user or config.SSH_USER
        last = ""
        for _ in (1, 2):
            try:
                conn = await self._get(host, user)
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(local_path, remote_path)
                if mode is not None:
                    await conn.run(f"chmod {mode:o} {shlex.quote(remote_path)}", check=False)
                return True
            except (asyncssh.Error, OSError) as e:
                last = str(e)
                await self._drop((user, host))
        logger.error("SFTP %s→%s@%s: %s", local_path, remote_path, host, last)
        return False

    async def bootstrap_run(self, host: str, password: str, uploads: list[tuple[str, str]],
                            command: str, timeout: int, echo=None) -> CmdResult:
        """ОДНОРАЗОВЫЙ парольный коннект root@host (вне ключевого кэша — provision-base
        выключает PasswordAuthentication, ключ появляется только после него). Заливает
        uploads [(local, remote)] по SFTP (chmod +x), затем стримит command. Соединение
        закрывается по выходу — этим паролем больше не пользуемся."""
        try:
            conn = await asyncssh.connect(
                host=host, port=config.SSH_PORT, username="root",
                password=password, known_hosts=None,
                connect_timeout=config.SSH_CONNECT_TIMEOUT,
            )
        except asyncssh.PermissionDenied:
            return CmdResult(False, 255, "", f"root@{host}: пароль отвергнут (или PasswordAuthentication уже off)")
        except (asyncssh.Error, OSError) as e:
            return CmdResult(False, 255, "", f"парольный коннект root@{host} не удался: {e}")
        try:
            async with conn.start_sftp_client() as sftp:
                for local, remote in uploads:
                    await sftp.put(local, remote)
                    await conn.run(f"chmod +x {shlex.quote(remote)}", check=False)
            return await self._stream(conn, command, timeout, echo)
        finally:
            conn.close()

    async def read_file(self, host: str, path: str) -> str | None:
        res = await self.run(host, f"cat {shlex.quote(path)}", timeout=15)
        return res.stdout if res.ok else None

    async def path_exists(self, host: str, path: str) -> bool:
        res = await self.run(host, f"test -e {shlex.quote(path)}", timeout=15)
        return res.ok

    async def ping(self, host: str) -> bool:
        res = await self.run(host, "echo ok", timeout=config.SSH_CONNECT_TIMEOUT)
        return res.ok and res.stdout == "ok"

    async def close_all(self) -> None:
        for key in list(self._conns):
            await self._drop(key)
