"""Async-автоматизация OTC (pocketoption) и TradingView. Порт apps/browser_app.py.

Селекторы pocket (sel) приходят dict'ом из settings/pocket.resolve_selectors(...) —
резолвятся из БД один раз в начале флоу. Поля логина — константы (settings/constant.py).

Функции возвращают (ok, msg) — диалоги/ошибки показывает вызывающий флоу (gui/flows.py),
а не сам браузерный слой. Не-фатальные шаги логируем warning (правило #8), не валим флоу.
"""
from playwright.async_api import Page, TimeoutError as PWTimeout

from tools.cookies.logs import init_logger
from tools.cookies.settings.constant import BUTTON_FIELD, MAIL_FIELD, PASSWORD_FIELD

logger = init_logger(__name__)
DEFAULT_TIMEOUT = 20_000  # ms


async def navigate(page: Page, url: str) -> tuple[bool, str]:
    try:
        await page.goto(url, wait_until="load", timeout=60_000)
        return True, ""
    except (Exception,) as error:
        return False, f"Ошибка загрузки страницы - {error}"


async def _js_click(locator, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Клик через JS — обходит strict actionability. Нужен для <a><i/></a>-обёрток и
    анимированных контролов графика."""
    await locator.wait_for(state="visible", timeout=timeout)
    await locator.scroll_into_view_if_needed(timeout=timeout)
    await locator.evaluate("el => el.click()")


async def full_auth_data(page: Page, data) -> tuple[bool, str]:
    """Заполнить почту/пароль и нажать «Войти». Возвращает (ok, msg)."""
    mail = data.get("mail")
    password = data.get("pocket_pass", "")
    if not mail:
        return False, "В данных отсутствует почта"

    try:
        await page.locator(f".{MAIL_FIELD} input").first.fill(mail, timeout=DEFAULT_TIMEOUT)
    except (Exception,) as error:
        return False, f"Ошибка поиска поля ввода почты - {error}"
    try:
        await page.locator(f".{PASSWORD_FIELD} input").first.fill(password, timeout=DEFAULT_TIMEOUT)
    except (Exception,) as error:
        return False, f"Ошибка поиска поля ввода пароля - {error}"
    try:
        await page.locator(f".{BUTTON_FIELD} button").first.click(timeout=DEFAULT_TIMEOUT)
    except (Exception,) as error:
        return False, f'Ошибка нажатия кнопки "Войти" - {error}'

    try:
        await page.locator(f".{MAIL_FIELD} input").first.wait_for(
            state="hidden", timeout=DEFAULT_TIMEOUT)
    except PWTimeout:
        logger.warning("full_auth_data: форма логина всё ещё видна после таймаута")

    await del_popup(page)
    return True, ""


async def del_popup(page: Page) -> None:
    try:
        await page.locator(".mfp-close").first.click(timeout=2_000)
    except (Exception,):
        pass  # попапа нет — норма (best-effort)


async def _wait_chart_ready(page: Page, sel: dict) -> None:
    """Дождаться отрисовки графика. PocketOption гидрирует виджет асинхронно после load —
    автоматизация раньше промахивается по несобранному DOM. Ждём появления якоря
    trade_window, затем короткая стабилизация (canvas дорисовывается асинхронно, надёжного
    DOM-сигнала нет — слепая пауза здесь оправдана)."""
    try:
        await page.locator(f".{sel['trade_window']}").first.wait_for(
            state="visible", timeout=30_000)
    except (Exception,) as error:
        logger.warning("_wait_chart_ready: trade_window не появился: %s", error)
    await page.wait_for_timeout(3_000)


async def _close_side_bar(page: Page, data_type: str) -> None:
    """Закрыть левый/правый бар. <a> оборачивает <i> — реальный клик попадает в <i> и
    обработчик <a> не срабатывает, поэтому JS-клик. Не-фатально (best-effort)."""
    try:
        bar = page.locator(f"a[data-type='{data_type}']").first
        await bar.wait_for(state="attached", timeout=DEFAULT_TIMEOUT)
        await bar.scroll_into_view_if_needed(timeout=DEFAULT_TIMEOUT)
        await bar.evaluate("el => el.click()")
    except (Exception,) as error:
        logger.warning("init_otc: не отключил бар '%s': %s", data_type, error)


async def init_otc(page: Page, sel: dict) -> tuple[bool, str]:
    """Авто-настройка OTC: закрыть бары, проверить trade_window, выставить таймфрейм/масштаб."""
    try:
        await _wait_chart_ready(page, sel)
        await _close_side_bar(page, "left")
        await _close_side_bar(page, "right")

        try:
            menu = page.locator(f".{sel['trade_window']}").first
            await menu.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
            color = await menu.evaluate("el => getComputedStyle(el).color")
            if color == "rgb(255, 255, 255)":
                await menu.click()
        except (Exception,) as error:
            logger.warning("init_otc: шаг trade_window пропущен: %s", error)

        ok = await design_customization(page, sel)
        if ok:
            return True, ""
        return False, "Не удалось настроить таймфреймы/масштаб"
    except (Exception,) as error:
        logger.warning("init_otc: ошибка: %s", error)
        return False, f"Ошибка авто-настройки OTC - {error}"


async def otc_list_close(page: Page, sel: dict) -> bool:
    """Закрыть панель настроек графика реальным кликом по координатам селектора.
    Программный DOM .click() НЕ триггерит document-level click-outside, на который
    рассчитана панель."""
    try:
        clicker = page.locator(sel["otc_val_list_close"]).first
        await clicker.wait_for(state="visible", timeout=10_000)
        await clicker.scroll_into_view_if_needed(timeout=10_000)
        await clicker.click(force=True, timeout=10_000)
        return True
    except PWTimeout:
        logger.warning("otc_list_close: фрагмент для закрытия меню не появился за 10с")
        return False
    except (Exception,) as error:
        logger.warning("otc_list_close: ошибка: %s", error)
        return False


async def design_customization(page: Page, sel: dict) -> bool:
    """Таймфрейм H4 + масштаб свечи S30. True только при полном успехе."""
    ok = True

    try:
        await _js_click(page.locator(f".{sel['timeframe_otc']}").first)
    except (Exception,) as error:
        logger.warning("design_customization: не открыл список таймфреймов: %s", error)
        ok = False

    try:
        await _js_click(page.locator(f"xpath={sel['change_tf']}").first)
    except (Exception,) as error:
        logger.warning("design_customization: не выбрал таймфрейм H4: %s", error)
        ok = False

    try:
        await _js_click(page.locator(f".{sel['chart_type']}").first)
    except (Exception,) as error:
        logger.warning("design_customization: не открыл окно масштаба свечи: %s", error)
        ok = False

    try:
        await _js_click(page.locator(sel["s30"], has_text="S30").first)
    except (Exception,) as error:
        logger.warning("design_customization: не выбрал масштаб свечи S30: %s", error)
        ok = False

    if not await otc_list_close(page, sel):
        ok = False
    return ok


async def cook_tv(page: Page) -> tuple[bool, str]:
    """TradingView: закрыть google-iframe (One Tap). Возвращает (ok, msg)."""
    try:
        iframe = page.frame_locator("iframe[src*='accounts.google.com/gsi/iframe/select']")
        await iframe.locator("#close").click(timeout=15_000)
        return True, ""
    except (Exception,) as error:
        logger.warning("cook_tv: не закрыл всплывающее окно Google: %s", error)
        return False, f"Ошибка закрытия всплывающего окна Google - {error}"
