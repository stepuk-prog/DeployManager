"""Локальный HTTP-прокси-релей с авторизацией для OTC-режима (binodex).

Firefox под Playwright НЕ умеет socks5-auth и ненадёжно жуёт http-авторизацию upstream-прокси
напрямую (proxy.username/password), поэтому поднимаем локальный релей на 127.0.0.1:<рандом-порт>,
который подставляет заголовок `Proxy-Authorization: Basic ...` и форвардит на удалённый
:50100-HTTP-прокси из settings.proxy_data. Браузеру отдаём адрес локального релея (без авторизации).

Релей — HTTP-only (форвардит сырой HTTP + инжектит заголовок); SOCKS5 (:50101) не умеет by
design. Перенос рабочего модуля из проекта Screens — без внешних зависимостей, чистый stdlib
(threading/socket/select).
"""

import threading
import socket
import base64
import select
import time
from typing import Optional

from tools.cookies.logs import init_logger

logger = init_logger(__name__)

LOCAL_PROXY_HOST = '127.0.0.1'


class ProxyAuthThread(threading.Thread):
    """Поток для обработки прокси-соединения с автоматической авторизацией."""

    def __init__(self, client_socket, remote_host, remote_port, username, password):
        super().__init__(daemon=True)
        self.client_socket = client_socket
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.username = username
        self.password = password

    def run(self):
        remote_socket = None
        try:
            # Первый запрос от клиента (с таймаутом — молчащий клиент не должен навсегда
            # подвесить поток и утечь сокетом).
            self.client_socket.settimeout(30)
            request = self.client_socket.recv(4096)
            if not request:
                return

            # Вставляем заголовок авторизации после первой строки запроса.
            request_str = request.decode('utf-8', errors='ignore')
            lines = request_str.split('\r\n')
            auth_string = f"{self.username}:{self.password}"
            auth_b64 = base64.b64encode(auth_string.encode('utf-8')).decode('ascii')
            auth_header = f"Proxy-Authorization: Basic {auth_b64}"
            if len(lines) > 1:
                lines.insert(1, auth_header)
                modified_request = '\r\n'.join(lines).encode('utf-8')
            else:
                modified_request = request

            # Подключаемся к удалённому прокси и шлём модифицированный запрос.
            remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_socket.settimeout(30)
            remote_socket.connect((self.remote_host, self.remote_port))
            remote_socket.sendall(modified_request)

            self.forward_data(self.client_socket, remote_socket)

        except (Exception,) as e:
            logger.debug(f"Ошибка в прокси-соединении: {e}")
        finally:
            try:
                self.client_socket.close()
            except (Exception,):
                pass
            if remote_socket:
                try:
                    remote_socket.close()
                except (Exception,):
                    pass

    @staticmethod
    def forward_data(client_sock, remote_sock):
        """Пересылает данные между клиентом и удалённым сервером."""
        sockets = [client_sock, remote_sock]
        timeout = 60
        while True:
            try:
                readable, _, exceptional = select.select(sockets, [], sockets, timeout)
                if exceptional:
                    break
                if not readable:
                    break
                for sock in readable:
                    data = sock.recv(8192)
                    if not data:
                        return
                    if sock is client_sock:
                        remote_sock.sendall(data)
                    else:
                        client_sock.sendall(data)
            except (Exception,):
                break


class LocalProxyServer:
    """Локальный прокси-сервер с авторизацией."""

    def __init__(self, remote_host, remote_port, username, password):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.username = username
        self.password = password
        self.server_socket = None
        self.running = False
        self.thread = None
        self.actual_port = None  # реальный порт, назначенный ОС

    def start(self):
        if self.running:
            return
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()

    def _run_server(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass  # SO_REUSEPORT не поддерживается на этой платформе
            # bind на порт 0 — ОС сама выделит свободный порт.
            self.server_socket.bind((LOCAL_PROXY_HOST, 0))
            self.actual_port = self.server_socket.getsockname()[1]
            self.server_socket.listen(50)
            self.server_socket.settimeout(1)
            self.running = True

            while self.running:
                try:
                    client_socket, _addr = self.server_socket.accept()
                    handler = ProxyAuthThread(
                        client_socket, self.remote_host, self.remote_port,
                        self.username, self.password
                    )
                    handler.start()
                except socket.timeout:
                    continue
                except (Exception,) as e:
                    if self.running:
                        logger.error(f"Ошибка приёма соединения локального прокси: {e}")
        except OSError as e:
            if getattr(e, 'errno', None) == 98:
                logger.error("Локальный прокси: порт уже используется")
            else:
                logger.error(f"Ошибка сокета при запуске локального прокси: {e}")
        except (Exception,) as e:
            logger.error(f"Ошибка запуска локального прокси: {e}")
        finally:
            if self.server_socket:
                try:
                    self.server_socket.close()
                except (Exception,):
                    pass

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except (Exception,) as e:
                logger.debug(f"Ошибка закрытия серверного сокета локального прокси: {e}")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)


# Текущий локальный прокси-сервер (один на процесс; пересоздаётся при смене прокси).
_current_proxy_server: Optional[LocalProxyServer] = None


def _test_proxy_port(host, port, timeout=0.5) -> bool:
    """Проверяет, что порт действительно слушает соединения."""
    try:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.settimeout(timeout)
        test_socket.connect((host, port))
        test_socket.close()
        return True
    except (Exception,):
        return False


def start_local_proxy(remote_host, remote_port, username, password):
    """Запускает локальный релей на динамически выделенном порту. (host, port) | (None, None)."""
    global _current_proxy_server
    if _current_proxy_server:
        _current_proxy_server.stop()
        time.sleep(0.3)  # даём корректно закрыться

    _current_proxy_server = LocalProxyServer(remote_host, remote_port, username, password)
    _current_proxy_server.start()

    max_wait, waited, sleep_step = 3.0, 0.0, 0.1
    while waited < max_wait:
        if _current_proxy_server.running and _current_proxy_server.actual_port:
            if _test_proxy_port(LOCAL_PROXY_HOST, _current_proxy_server.actual_port):
                logger.debug(f"Локальный прокси запущен на порту {_current_proxy_server.actual_port}")
                return LOCAL_PROXY_HOST, _current_proxy_server.actual_port
        time.sleep(sleep_step)
        waited += sleep_step

    logger.error("Не удалось запустить локальный прокси-сервер")
    return None, None


def stop_local_proxy():
    """Останавливает локальный прокси-сервер (graceful-shutdown)."""
    global _current_proxy_server
    if _current_proxy_server:
        _current_proxy_server.stop()
        _current_proxy_server = None
