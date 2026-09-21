"""Вход на binodex.app (email-OTP) и настройка сайта для вкладки «Cookies».

Сам вход — в ЯДРЕ (`binocore.binodex`), тут только обёртка. До 21-09-2026 здесь лежала
тринадцатая копия логина, причём доядерная: письмо искалось только от `privy.io`, код читался
только из ТЕЛА письма, а признаком входа считался единственный ключ `privy:token`. binodex с
18-09-2026 раскатывает СВОЮ авторизацию (флаг `my.ownAuth`, A/B по нодам): письмо приходит с
`account@mail.binodex.io`, код стоит в ТЕМЕ, в localStorage ложится `ownAuthSession`. На таком
аккаунте инструмент «не видел почту» — досиживал 120с и падал по таймауту, хотя письмо лежало
в ящике. Ядро держит оба механизма сразу и читает отправителей/тему/ключи сессии из
`binodex_settings`, поэтому следующий ход binodex правится один раз на всю семью.

Настройка сайта (масштабы, окно Welcome) осталась здесь: она нужна только этому инструменту —
боевые программы правят график своим `ensure_chart_setup` перед каждым опционом.
"""
from playwright.async_api import Page

from binocore import binodex as core
from tools.cookies.logs import init_logger
from tools.cookies.settings.constant import SETUP_STEPS

logger = init_logger(__name__)


def missing_login_selectors(sel: dict) -> list[str]:
    """Каких обязательных селекторов логина не хватает (пусто = всё ок).

    Список ЯДРОВЫЙ — инструмент и программы обязаны требовать одно и то же. Проверяем до
    запуска браузера: ядро откажется и само, но оператору дырку в `binodex_settings` полезнее
    увидеть раньше, чем откроется окно."""
    return [k for k in core.REQUIRED_SELECTORS if not sel.get(k)]


class _StatusLog:
    """Логгер для ядра, который пишет И в файл инструмента, И строкой в окно визарда.

    Ядро логирует ход входа (отказ модалки с её же текстом, «binodex сам увёл на /trade»,
    успех) — оператору у видимого браузера это ровно то, что нужно видеть. Уровень `report`
    ядро зовёт через getattr, так что его отсутствие безопасно: успех уйдёт в info."""

    def __init__(self, status) -> None:
        self._status = status

    def _emit(self, prefix: str, message, args) -> None:
        text = str(message) % args if args else str(message)
        logger.info("binodex-логин: %s", text)
        try:
            self._status(f"{prefix}{text}")
        except (Exception,):
            pass          # визард могли закрыть — вход из-за этого прерывать незачем

    def debug(self, message, *args) -> None:
        logger.debug("binodex-логин: %s", str(message) % args if args else message)

    def info(self, message, *args) -> None:
        self._emit("", message, args)

    def warning(self, message, *args) -> None:
        self._emit("⚠️ ", message, args)

    def error(self, message, *args) -> None:
        self._emit("❌ ", message, args)


async def inline_login(page: Page, sel: dict, mail: str, app_pass: str, status=None) -> bool:
    """Войти по одноразовому коду с почты в ТЕКУЩЕМ (видимом) окне. True — вошли.

    Обычный сбой ядро не бросает, а возвращает False (исключение одно — отказ по лимиту
    запросов кода, `core.LoginRateLimited`): вызывающий тогда оставляет окно открытым, чтобы
    оператор доделал вход руками. Письма с одноразовыми кодами ядро убирает само — но только
    после доказанного входа."""
    log = _StatusLog(status) if status is not None else logger
    return await core.inline_login(page, page.context, mail=mail, app_pass=app_pass,
                                   sel=sel, logger=log)


async def dismiss_welcome(page: Page) -> None:
    """Закрыть приветственное окно ('Welcome!' со Skip/View guide), которое binodex
    показывает новым аккаунтам после первого входа. Оно перекрывает график → клики
    настройки попадают по модалке и молча падают. Селектора в БД нет → ищем по тексту/роли."""
    try:
        await page.get_by_role("button", name="Skip", exact=True).first.click(timeout=8_000)
        await page.wait_for_timeout(500)
    except (Exception,):
        try:    # фолбэк: кнопка-крестик в шапке модалки
            await page.get_by_text("Skip", exact=True).first.click(timeout=3_000)
            await page.wait_for_timeout(500)
        except (Exception,):
            pass


async def setup_site(page: Page, sel: dict) -> None:
    """Прокликать настройку сайта (масштаб свечи/графика) и закрыть окно. Разовый флоу:
    слепые паузы оправданы — ждём анимации меню, надёжного DOM-сигнала у дропдаунов нет.
    Выбор темы НЕ трогаем (по указанию — тему оставляем как есть)."""
    await page.wait_for_timeout(2_500)
    await dismiss_welcome(page)   # у новых аккаунтов окно Welcome перекрывает график
    for open_key, item_key in SETUP_STEPS:
        try:
            await page.locator(sel[open_key]).first.click(timeout=8_000)
            await page.locator(sel[item_key]).first.click(timeout=8_000)
            await page.wait_for_timeout(500)
        except (Exception,):
            pass
