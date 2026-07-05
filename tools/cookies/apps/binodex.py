"""Async-автоматизация логина binodex.app (Privy email-OTP) + настройка сайта. Порт
BinoOptions apps/binodex_session.py на async (тут — в Flet-цикле, без подпроцесса:
браузер видимый, при сбое окно не закрываем → ручное вмешательство).

Шаги разнесены, потому что между отправкой e-mail и вводом кода флоу ждёт письмо через
IMAP (apps/imap_code.py, в asyncio.to_thread). Селекторы (sel) — из БД (db.binodex_selectors).
"""
from playwright.async_api import Page, TimeoutError as PWTimeout

from tools.cookies.logs import init_logger
from tools.cookies.settings import config
from tools.cookies.settings.constant import REQUIRED_LOGIN_SELECTORS, SETUP_STEPS

logger = init_logger(__name__)


def missing_login_selectors(sel: dict) -> list[str]:
    """Каких обязательных селекторов логина не хватает (пусто = всё ок)."""
    return [k for k in REQUIRED_LOGIN_SELECTORS if not sel.get(k)]


async def _wait_code_step(page: Page, sel: dict, timeout: int) -> None:
    """Шаг ввода кода: в DOM стало ≥6 input'ов (на шаге e-mail — один)."""
    await page.wait_for_function(
        "s => document.querySelectorAll(s).length >= 6",
        arg=sel["login_code_inputs"], timeout=timeout)


async def open_and_send_email(page: Page, sel: dict, mail: str) -> None:
    """Открыть binodex → ввести e-mail → отправить (Enter, фолбэк — клик login_submit).
    Возвращается, когда появились ячейки кода."""
    await page.goto(config.BINODEX_LANDING, wait_until="domcontentloaded", timeout=30_000)
    await page.click(sel["login_open"], timeout=15_000)
    await page.fill(sel["login_email"], mail, timeout=15_000)
    await page.locator(sel["login_email"]).press("Enter")   # отправка надёжнее через Enter
    try:
        await _wait_code_step(page, sel, timeout=8_000)
    except PWTimeout:
        await page.locator(sel["login_submit"]).first.click(timeout=8_000)
        await _wait_code_step(page, sel, timeout=15_000)


async def enter_code(page: Page, sel: dict, code: str) -> None:
    """Ввести 6-значный код (keyboard.type — OTP-виджет раскидает; фолбэк — по цифре)."""
    cells = page.locator(sel["login_code_inputs"])
    if await cells.count() < 6:
        raise RuntimeError(f"ожидал 6 ячеек кода, нашёл {await cells.count()}")
    await cells.first.click()
    await page.keyboard.type(code, delay=60)
    if await cells.first.input_value() != code[0]:
        for i, ch in enumerate(code):
            await cells.nth(i).fill(ch)


async def finish_login(page: Page) -> None:
    """Privy больше НЕ редиректит на /trade. Признак входа = localStorage['privy:token'];
    дождавшись, сами идём на /trade и проверяем, что не выбросило обратно."""
    await page.wait_for_function(
        "() => !!window.localStorage.getItem('privy:token')", timeout=30_000)
    await page.goto(config.BINODEX_TRADE, wait_until="domcontentloaded", timeout=30_000)
    if not page.url.rstrip("/").endswith("/trade"):
        raise RuntimeError(f"после логина редирект с /trade на {page.url}")


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
    """Прокликать настройку сайта (масштаб свечи/графика + тема) и закрыть окно. Разовый
    флоу: слепые паузы оправданы — ждём анимации меню, надёжного DOM-сигнала у дропдаунов нет."""
    await page.wait_for_timeout(2_500)
    await dismiss_welcome(page)   # у новых аккаунтов окно Welcome перекрывает график
    for open_key, item_key in SETUP_STEPS:
        try:
            await page.locator(sel[open_key]).first.click(timeout=8_000)
            await page.locator(sel[item_key]).first.click(timeout=8_000)
            await page.wait_for_timeout(500)
        except (Exception,):
            pass
    try:
        await page.locator(sel["setup_settings_open"]).first.click(timeout=8_000)
        await page.locator(sel["setup_theme"]).first.click(timeout=8_000)
        await page.locator(sel["setup_theme_toggle"]).first.click(timeout=8_000)
        await page.wait_for_timeout(500)
        await page.locator(sel["setup_settings_open"]).first.click(timeout=8_000)  # закрыть
    except (Exception,):
        pass
