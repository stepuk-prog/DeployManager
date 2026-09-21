"""Телеграм-слой ядра. Пока один сюжет: жив ли Premium у юзербота.

Кастом-эмодзи (`MessageEntityCustomEmoji`) по MTProto может слать ТОЛЬКО Premium-аккаунт.
Семь программ семьи постят их юзерботом, и ни в одной не было проверки `is_premium` — то есть
истечение Premium ломало оформление молча: посты либо уходили без нужных эмодзи, либо Telegram
отвечал `PREMIUM_ACCOUNT_REQUIRED`, и это выглядело как обычный сбой отправки.

Проверка живёт здесь, а не в семи копиях `apps/tg_send.py`, ровно по той причине, по которой
сюда переехали images и db: одну и ту же правку иначе приходится вносить семь раз (на 5xx от
Telegram мы на этом уже обожглись 2026-09-11).

Что делает и чего НЕ делает:
  * дёргает `get_me()` и смотрит `is_premium` — на старте и дальше не чаще, чем раз в
    `interval` (по умолчанию 2 часа);
  * ведёт отметку в БД (`telegram.telegram.premium`) через переданный `mark`: ставит и СНИМАЕТ,
    то есть таблица отражает факт, а не историю ручных правок;
  * шлёт предупреждение в отдельную тему форума ошибок через переданный `notify` — повторяя
    его на каждой проверке, пока Premium нет;
  * НЕ трогает сам пост: решение владельца — слать как есть. Задача слоя в том, чтобы о потере
    Premium узнали, а не чтобы он молча менял оформление постов.

Ни pyrogram, ни aiogram, ни SQL здесь нет намеренно: клиент берётся уткой (`client.get_me()`),
а запись в БД и отправка уведомления приходят колбэками от программы. Поэтому модуль
импортируется и в программах на aiogram, и в тестах без сетевого клиента, а транспорт
переиспользует уже поднятые соединения программы — второй сессии бота не появляется.
"""
import asyncio
import logging
import time

# Логгер программы подключается через configure() — как в binocore.images: у семьи свой
# init_logger (файлы по уровням + отправка в Telegram), и писать надо именно в него.
_logger = logging.getLogger(__name__)

# Интервал повторной проверки: Premium истекает посреди прогона, а программы живут неделями,
# поэтому проверки только на старте мало. Два часа — решение владельца (2026-09-11).
DEFAULT_INTERVAL = 2 * 60 * 60

# Потолок на get_me(). Проба живости в программах семьи стоит с таким же порядком величины:
# висящий запрос не должен подвешивать старт.
DEFAULT_TIMEOUT = 15

_ALERT = ('‼️ У аккаунта {account} НЕТ Premium — Telegram не пропустит кастом-эмодзи в постах '
          '{program}. Посты продолжают уходить как есть; чинится продлением Premium.')
_RESTORED = 'Premium у аккаунта {account} снова активен — оформление постов {program} в норме.'


def configure(logger=None) -> None:
    """Подключить логгер программы. Зовётся один раз на старте, до первой проверки."""
    global _logger
    if logger is not None:
        _logger = logger


async def account_is_premium(client, *, timeout: float = DEFAULT_TIMEOUT) -> bool | None:
    """`is_premium` аккаунта или None, если спросить не удалось.

    Три исхода, и различать их обязательно: True/False — ответ Telegram, None — мы не знаем
    (сеть, таймаут, клиент не поднят). None НЕ означает «Premium нет»: принять обрыв связи за
    потерю Premium значит поднять ложную тревогу ровно в тот момент, когда программе и так
    плохо, да ещё и снять отметку в БД по чужой вине."""
    try:
        me = await asyncio.wait_for(client.get_me(), timeout=timeout)
    except (Exception,):
        return None
    return bool(getattr(me, 'is_premium', False))


class PremiumGuard:
    """Периодическая проверка Premium: отметка в БД + предупреждение в тему форума ошибок.

    Держит состояние между вызовами, поэтому создаётся ОДИН на процесс (модульная переменная
    рядом с клиентом). Зовётся из уже существующих точек: проба живости юзербота на старте и
    дальше любой регулярный цикл — `ensure` сам решит, пора ли спрашивать Telegram.

    `mark(premium: bool)` — записать отметку в БД (`telegram.telegram.premium`). Зовётся только
    когда значение ИЗМЕНИЛОСЬ относительно последнего известного, чтобы не писать в таблицу
    каждые два часа одно и то же. На первой проверке после старта значение неизвестно, поэтому
    пишем всегда — это же и лечит расхождение таблицы с реальностью.

    `notify(text: str)` — отправить предупреждение в отдельную тему форума ошибок. Если не
    передан, текст уходит в `logger.error` программы (у семьи ERROR и так попадает в тему
    ошибок — хуже адресом, но не молча).
    """

    def __init__(self, *, mark=None, notify=None, program: str = '',
                 account: str = 'юзербот', interval: float = DEFAULT_INTERVAL,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.mark = mark
        self.notify = notify
        self.program = program
        self.account = account
        self.interval = interval
        self.timeout = timeout
        self.state: bool | None = None      # последний ИЗВЕСТНЫЙ ответ Telegram
        self._checked_at: float | None = None

    def due(self, now: float | None = None) -> bool:
        """Пора ли спрашивать. Первый вызов — всегда да (это и есть проверка на старте)."""
        if self._checked_at is None:
            return True
        return (now if now is not None else time.monotonic()) - self._checked_at >= self.interval

    async def ensure(self, client, *, force: bool = False) -> bool | None:
        """Проверить (если пора), обновить отметку и уведомить. Возвращает текущее знание."""
        if not force and not self.due():
            return self.state

        premium = await account_is_premium(client, timeout=self.timeout)
        if premium is None:
            # Спросить не вышло — не трогаем ни состояние, ни таймер, ни БД: иначе неудачная
            # проба «съела» бы окно, и настоящая потеря Premium ждала бы следующего срока.
            _logger.warning(f'Premium {self.account}: проверить не удалось (нет ответа на '
                            f'get_me за {self.timeout}с) — состояние прежнее')
            return self.state

        was, self.state = self.state, premium
        changed = premium != was
        self._checked_at = time.monotonic()

        if changed and self.mark is not None:
            try:
                await self.mark(premium)
            except (Exception,) as error:
                # Отметка в БД — не повод потерять уведомление: оно важнее строки в таблице.
                _logger.warning(f'Premium {self.account}: отметку в БД обновить не удалось '
                                f'({type(error).__name__}: {error})')

        if not premium:
            await self._say(_ALERT.format(account=self.account, program=self.program), alert=True)
        elif was is False:
            # Именно False, а не «не True»: на первой проверке состояние неизвестно (None), и
            # сообщать о «восстановлении» там нечего — терять было нечего.
            await self._say(_RESTORED.format(account=self.account, program=self.program))
        return premium

    async def _say(self, text: str, *, alert: bool = False) -> None:
        """Уведомление в выделенную тему; при её отсутствии или сбое — в лог программы."""
        if self.notify is not None:
            try:
                await self.notify(text)
                return
            except (Exception,) as error:
                _logger.warning(f'Premium {self.account}: уведомление в тему не ушло '
                                f'({type(error).__name__}: {error}) — пишу в лог')
        (_logger.error if alert else _logger.warning)(text)
