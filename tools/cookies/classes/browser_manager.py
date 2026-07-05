"""Жизненный цикл async-Playwright (Firefox) в том же event-loop, что и Flet.

sync-Playwright нельзя крутить в asyncio → берём async_playwright. Браузер видимый
(headless из *_LAUNCH_OPTIONS), при сбое флоу окно НЕ закрываем (ручное вмешательство).

Один движок Playwright на всё приложение (start/shutdown), отдельные browser+context+page
на каждый флоу (launch → закрытие через BrowserSession.close()).
"""
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from tools.cookies.logs import init_logger

logger = init_logger(__name__)


@dataclass
class BrowserSession:
    """Один сеанс: browser + context + page. close() гасит и логирует best-effort."""
    browser: Browser
    context: BrowserContext
    page: Page

    async def close(self) -> None:
        for what, obj in (("context", self.context), ("browser", self.browser)):
            try:
                await obj.close()
            except (Exception,) as error:
                logger.warning("Ошибка закрытия %s: %s", what, error)


class BrowserManager:
    def __init__(self):
        self._pw = None  # AsyncPlaywright

    async def start(self) -> None:
        """Поднять движок Playwright (идемпотентно)."""
        if self._pw is None:
            self._pw = await async_playwright().start()
            logger.info("Playwright запущен")

    async def launch(self, launch_options: dict, context_options: dict,
                     storage_state: dict | None = None) -> BrowserSession:
        """Открыть firefox + context (+ storage_state) + page."""
        await self.start()
        browser = await self._pw.firefox.launch(**launch_options)
        ctx_kwargs = dict(context_options)
        if storage_state is not None:
            ctx_kwargs["storage_state"] = storage_state
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        return BrowserSession(browser=browser, context=context, page=page)

    async def shutdown(self) -> None:
        """Погасить движок Playwright (при закрытии приложения)."""
        if self._pw is not None:
            try:
                await self._pw.stop()
                logger.info("Playwright остановлен")
            except (Exception,) as error:
                logger.warning("Ошибка остановки Playwright: %s", error)
            self._pw = None
