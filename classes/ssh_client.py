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
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _get(self, host: str) -> asyncssh.SSHClientConnection:
        async with self._locks[host]:
            conn = self._conns.get(host)
            if conn is None:
                conn = await asyncssh.connect(
                    host=host, port=config.SSH_PORT, username=config.SSH_USER,
                    client_keys=[config.SSH_KEY], known_hosts=None,
                    connect_timeout=config.SSH_CONNECT_TIMEOUT,
                )
                self._conns[host] = conn
            return conn

    async def _drop(self, host: str) -> None:
        conn = self._conns.pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    async def run(self, host: str, command: str, timeout: int = 30, sudo: bool = False) -> CmdResult:
        """Команда на ноде. sudo=True → префикс `sudo -n` (passwordless, без TTY)."""
        cmd = f"sudo -n {command}" if sudo else command
        last = ""
        for attempt in (1, 2):  # одна попытка реконнекта при обрыве соединения
            try:
                conn = await self._get(host)
                res = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
                ex = res.exit_status if res.exit_status is not None else 1
                return CmdResult(ex == 0, ex, (res.stdout or "").strip(), (res.stderr or "").strip())
            except asyncio.TimeoutError:
                logger.error("SSH %s: таймаут %ss на %r", host, timeout, command)
                return CmdResult(False, 255, "", f"timeout {timeout}s")
            except (asyncssh.Error, OSError) as e:
                last = str(e)
                await self._drop(host)  # на 2-й попытке создастся новое соединение
        logger.error("SSH %s: ошибка %r — %s", host, command, last)
        return CmdResult(False, 255, "", last)

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
        for host in list(self._conns):
            await self._drop(host)
