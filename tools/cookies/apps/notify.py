"""Уведомления в Telegram через Bot API. Без тяжёлой зависимости: POST через urllib в
asyncio.to_thread (urllib блокирующий). Таймаут на запрос (правило #6)."""
import asyncio
import urllib.parse
import urllib.request

from tools.cookies.logs import init_logger
from tools.cookies.settings import config

logger = init_logger(__name__)


def _post(text: str) -> None:
    if not config.TG_TOKEN or not config.TG_CHANNEL:
        logger.warning("notify: TG_TOKEN/TG_CHANNEL не заданы — уведомление пропущено")
        return
    url = f"https://api.telegram.org/bot{config.TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": config.TG_CHANNEL, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            resp.read()
    except (Exception,) as error:
        logger.warning("notify: ошибка отправки в Telegram: %s", error)


async def notify(text: str) -> None:
    await asyncio.to_thread(_post, text)
