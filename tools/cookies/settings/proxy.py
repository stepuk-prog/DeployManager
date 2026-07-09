"""Подбор прокси для сбора cookies binodex — как в BinoOptions (settings/proxy.py).

Прокси берутся из settings.proxy_data в БД binodex (общий пул семейства ботов). BROWSER
(Playwright-Firefox) НЕ умеет socks5-auth и ненадёжно жуёт http-auth напрямую, поэтому берём
ТОЛЬКО :50100 (HTTP) и авторизуемся через локальный релей (settings/local_proxy) — браузеру
отдаём адрес релея без авторизации.

Cookies-инструмент всегда в OTC-контексте binodex → scope фиксированный 'binodex' (поля
бана *_binodex). Здесь только ВЫБОР прокси (без stats/ban — сбор cookies разовый, ручной).
"""
import random
from dataclasses import dataclass
from typing import Optional

from tools.cookies.logs import init_logger

logger = init_logger(__name__)

PROXY_SCOPE = "binodex"


@dataclass
class ProxyData:
    """Данные прокси из settings.proxy_data (:50100 HTTP)."""
    ip: str
    port: int
    login: str
    password: str


# Активные прокси из БД (кэш на процесс) и уже опробованные в этом сеансе.
proxy_list: list[ProxyData] = []
used_proxies: set[str] = set()
current_proxy: Optional[ProxyData] = None


async def load_proxies_from_db(database) -> bool:
    """Загрузка активных :50100-прокси (scope binodex) из БД. True при успехе."""
    global proxy_list
    rows = await database.get_active_proxies(PROXY_SCOPE)
    if not rows:  # None/False/[] — пула нет или сбой
        logger.error("Прокси(%s): не удалось загрузить активные :50100 из settings.proxy_data", PROXY_SCOPE)
        proxy_list = []
        return False
    proxy_list = [ProxyData(ip=r["ip"], port=r["port"], login=r["login"], password=r["password"])
                  for r in rows]
    logger.info("Прокси(%s): загружено %d активных :50100-прокси из БД", PROXY_SCOPE, len(proxy_list))
    return True


def get_unused_proxy() -> Optional[ProxyData]:
    """Случайный ещё не опробованный прокси (по кругу). Выставляет current_proxy. None — пул пуст."""
    global current_proxy
    if not proxy_list:
        logger.error("Прокси(%s): список пуст — вызовите load_proxies_from_db() сначала", PROXY_SCOPE)
        current_proxy = None
        return None
    available = [p for p in proxy_list if p.ip not in used_proxies]
    if not available:  # все опробованы — начинаем круг заново
        used_proxies.clear()
        available = proxy_list.copy()
    proxy = random.choice(available)
    used_proxies.add(proxy.ip)
    current_proxy = proxy
    return proxy


def get_current_proxy() -> Optional[ProxyData]:
    """Текущий выбранный прокси (геттер — не ловить stale-binding модуля)."""
    return current_proxy
