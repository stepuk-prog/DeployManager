"""Flet-окно CookiesProgram2: вкладки OTC Option / OTC Screen / TradingView / Binodex.

Списки аккаунтов грузятся из БД, клик по кнопке аккаунта запускает соответствующий флоу
(gui/flows.py) в видимом браузере. Внизу — общая лог-панель (stdout/логгер → панель).
"""
import sys

import flet as ft

from tools.cookies.classes import BrowserManager
from tools.cookies.database import Database
from tools.cookies.gui import flows
from tools.cookies.gui.backend import FletUi
from tools.cookies.gui.flows import FlowContext
from tools.cookies.gui.log_sink import LogSink
from tools.cookies.logs import init_logger

logger = init_logger(__name__)


async def build_screen(page: ft.Page, on_back):
    """Построить экран Cookies поверх существующей страницы (встраивание в окно DeployManager).

    Вместо самостоятельного `page.add` в standalone-режиме: регистрирует свои сервисы/лог/БД,
    добавляет корневую колонку с кнопкой «← Назад» и возвращает `teardown()` — корутину,
    которую навигатор зовёт при выходе (восстановить stdout, закрыть пулы БД и браузер, снять
    clipboard-сервис). `on_back` — корутина возврата на домашний экран DeployManager.
    """
    # stdout вернём в teardown — иначе после «Назад» лог/print уйдут в мёртвую панель.
    old_stdout = sys.stdout

    # --- буфер обмена (для копирования почты/пароля из модалок) ---
    clipboard = ft.Clipboard()
    added_clipboard = False
    try:
        page.services.append(clipboard)
        added_clipboard = True
    except (Exception,):
        page.services = [clipboard]
        added_clipboard = True

    def copy(text: str) -> None:
        page.run_task(clipboard.set, text or "")

    # --- лог-панель (stdout/логгер пишут сюда) ---
    log_view = ft.ListView(expand=True, auto_scroll=True, spacing=1)
    sink = LogSink(log_view, page)
    sys.stdout = sink   # _StdoutProxy логгера читает текущий sys.stdout → строки в панель

    # --- инфраструктура ---
    db = Database()
    browser = BrowserManager()
    ui = FletUi(page)
    ctx = FlowContext(page=page, ui=ui, db=db, browser=browser, copy=copy)

    print("Подключение к БД…")
    try:
        await db.connect()
    except (Exception,) as error:
        print(f"❌ Не удалось подключиться к БД: {error}")

    # ---------- построение вкладок ----------
    def account_button(label: str, on_click) -> ft.Control:
        return ft.Button(content=ft.Text(label, text_align=ft.TextAlign.CENTER,
                                         max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                         tooltip=label, on_click=on_click, width=250, height=56)

    def list_column() -> ft.Column:
        return ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True, spacing=6)

    def add_grid(col: ft.Column, buttons: list, per_row: int = 4) -> None:
        """Разложить кнопки по `per_row` в ряд (по умолчанию 4) — чтобы все помещались."""
        for i in range(0, len(buttons), per_row):
            col.controls.append(ft.Row(buttons[i:i + per_row], spacing=6))

    def run_flow(coro_fn, *args):
        return lambda e: page.run_task(coro_fn, *args)

    otc_list = list_column()
    screen_list = list_column()
    tv_list = list_column()
    bnd_list = list_column()

    def _otc_label(d: dict) -> str:
        return d.get("program_name") or d.get("name") or f"User {d.get('cookies_pocket')}"

    async def load_otc_option(_=None):
        otc_list.controls.clear()
        otc_list.controls.append(ft.Text("OTC Option", weight=ft.FontWeight.BOLD))
        rows = await db.find_otc_option_cookies()
        add_grid(otc_list, [
            account_button(_otc_label(d := dict(acc)),
                           run_flow(flows.otc_flow, ctx, d, "OTC Option"))
            for acc in (rows or [])])
        other = await db.find_otc_other_cookies()
        if other:
            otc_list.controls.append(ft.Text("Прочее", weight=ft.FontWeight.BOLD))
            add_grid(otc_list, [
                account_button(_otc_label(d := dict(acc)),
                               run_flow(flows.otc_flow, ctx, d, "OTC Option"))
                for acc in other])
        page.update()

    async def load_otc_screen(_=None):
        screen_list.controls.clear()
        screen_list.controls.append(ft.Text("OTC Screen", weight=ft.FontWeight.BOLD))
        rows = await db.find_otc_screen_cookies(meta=False)
        add_grid(screen_list, [
            account_button(_otc_label(d := dict(acc)),
                           run_flow(flows.otc_flow, ctx, d, "OTC Screen"))
            for acc in (rows or [])])
        page.update()

    async def load_tv_old(_=None):
        tv_list.controls.clear()
        tv_list.controls.append(ft.Text("TradingView — Обновить старый", weight=ft.FontWeight.BOLD))
        rows = await db.find_tv_cookies()
        add_grid(tv_list, [
            account_button((d := dict(acc)).get("google_account_name") or f"User {d.get('user_id')}",
                           run_flow(flows.tv_flow, ctx, d))
            for acc in (rows or [])])
        page.update()

    async def load_tv_new(_=None):
        tv_list.controls.clear()
        tv_list.controls.append(ft.Text("TradingView — Добавить новый", weight=ft.FontWeight.BOLD))
        print("TradingView «Добавить новый»: критерий — в telegram.telegram заполнен tv_pass "
              "И ещё нет строки в cookies.tv_cookies. Чтобы аккаунт появился в списке, задайте "
              "ему tv_pass (и mail — для отображения/уведомления).")
        rows = await db.tv_new_accounts()
        buttons = []
        for r in (rows or []):
            acc = {"user_id": r["id_telegram"], "google_account_name": r["name"],
                   "mail": r["mail"], "tv_pass": r["tv_pass"]}
            label = r["name"] or f"User {r['id_telegram']}"
            buttons.append(account_button(
                label, run_flow(flows.tv_flow, ctx, acc, load_tv_new)))
        add_grid(tv_list, buttons)
        print(f"  → кандидатов для новых TV-cookies: {len(buttons)}")
        page.update()

    async def load_binodex_old(_=None):
        bnd_list.controls.clear()
        bnd_list.controls.append(ft.Text("Binodex — Options", weight=ft.FontWeight.BOLD))
        rows = await db.binodex_option_accounts()
        rows = rows or []
        pids = [r["program_id"] for r in rows if r["program_id"] is not None]
        names = await db.get_program_names(pids)
        name_map = {r["program_id"]: r["program_name"] for r in (names or [])}
        buttons = []
        for r in rows:
            cp = r["cookies_pocket"]
            label = name_map.get(r["program_id"]) or r["prog_name"] or f"User {cp}"
            buttons.append(account_button(
                label, run_flow(flows.binodex_flow, ctx, {"user_id": cp, "name": label}, "old")))
        add_grid(bnd_list, buttons)
        page.update()

    async def load_binodex_new(_=None):
        bnd_list.controls.clear()
        bnd_list.controls.append(ft.Text("Binodex — Добавить новый", weight=ft.FontWeight.BOLD))
        print("Binodex «Добавить новый»: критерий — в telegram.telegram заполнены mail и "
              "mail_app_pass (16-симв. Gmail app-password) И ещё нет строки в "
              "binodex.cookies.binodex_cookies. Чтобы аккаунт появился в списке, заполните эти поля.")
        tn = await db.telegram_new_accounts()
        existing = await db.binodex_existing_user_ids()
        existing_ids = {r["user_id"] for r in (existing or [])}
        buttons = []
        for r in (tn or []):
            if r["id_telegram"] in existing_ids:
                continue
            acc = {"user_id": r["id_telegram"], "name": r["name"],
                   "mail": r["mail"], "mail_app_pass": r["mail_app_pass"]}
            buttons.append(account_button(
                f"{r['name']} ({r['mail']})",
                run_flow(flows.binodex_flow, ctx, acc, "new", load_binodex_new)))
        add_grid(bnd_list, buttons)
        print(f"  → кандидатов для новых Binodex-cookies: {len(buttons)}")
        page.update()

    async def _load_binodex_category(title: str, user_ids: list):
        """Список аккаунтов категории (OTC Screen / Crypta Screen) → кнопки создания кук.
        Подпись — program_name по binodex-аккаунту (cookies_binodex); флоу тот же (mode='old')."""
        bnd_list.controls.clear()
        bnd_list.controls.append(ft.Text(title, weight=ft.FontWeight.BOLD))
        names = await db.program_names_by_account(user_ids)
        name_map = {r["cookies_binodex"]: r["program_name"] for r in (names or [])}
        buttons = []
        for uid in user_ids:
            label = name_map.get(uid) or f"User {uid}"
            buttons.append(account_button(
                label, run_flow(flows.binodex_flow, ctx, {"user_id": uid, "name": label}, "old")))
        add_grid(bnd_list, buttons)
        print(f"  → аккаунтов в «{title}»: {len(buttons)}")
        page.update()

    async def load_binodex_otc_screen(_=None):
        rows = await db.binodex_otc_screen_accounts()
        await _load_binodex_category("Binodex — OTC Screen",
                                     [r["user_id"] for r in (rows or [])])

    async def load_binodex_crypta(_=None):
        rows = await db.binodex_crypto_accounts()
        await _load_binodex_category("Binodex — Crypta Screen",
                                     [r["user_id"] for r in (rows or [])])

    def header(title: str, refresh) -> ft.Row:
        return ft.Row([
            ft.Text(title, weight=ft.FontWeight.BOLD, size=16, expand=True),
            ft.Button(content=ft.Text("🔄 Обновить список"), on_click=refresh),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    def tab_view(title: str, refresh, body: ft.Control) -> ft.Container:
        return ft.Container(expand=True, padding=8, content=ft.Column(
            [header(title, refresh), ft.Divider(), body], expand=True, spacing=8))

    # TradingView: два режима (как у Binodex). «Обновить старый» — аккаунты с уже сохранёнными
    # cookies; «Добавить новый» — у кого есть tv_pass, но строки в tv_cookies ещё нет.
    tv_header = ft.Row([
        ft.Text("TradingView", weight=ft.FontWeight.BOLD, size=16, expand=True),
        ft.Button(content=ft.Text("Обновить старый"), on_click=load_tv_old),
        ft.Button(content=ft.Text("Добавить новый"), on_click=load_tv_new),
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    tv_view = ft.Container(expand=True, padding=8, content=ft.Column(
        [tv_header, ft.Divider(), tv_list], expand=True, spacing=8))

    # Binodex: три категории (Options / OTC Screen / Crypta Screen) + «Добавить новый».
    # Категория = свой источник аккаунтов (option_setting / screen_otc / screen_crypto),
    # переключает список в одной области; флоу создания кук общий (Privy).
    bnd_header = ft.Row([
        ft.Text("Binodex", weight=ft.FontWeight.BOLD, size=16, expand=True),
        ft.Button(content=ft.Text("Options"), on_click=load_binodex_old),
        ft.Button(content=ft.Text("OTC Screen"), on_click=load_binodex_otc_screen),
        ft.Button(content=ft.Text("Crypta Screen"), on_click=load_binodex_crypta),
        ft.Button(content=ft.Text("Добавить новый"), on_click=load_binodex_new),
    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    bnd_view = ft.Container(expand=True, padding=8, content=ft.Column(
        [bnd_header, ft.Divider(), bnd_list], expand=True, spacing=8))

    tabs = ft.Tabs(length=4, expand=True, content=ft.Column(expand=True, controls=[
        ft.TabBar(tabs=[
            ft.Tab(label="OTC Option"),
            ft.Tab(label="OTC Screen"),
            ft.Tab(label="TradingView"),
            ft.Tab(label="Binodex"),
        ]),
        ft.TabBarView(expand=True, controls=[
            tab_view("OTC Option", load_otc_option, otc_list),
            tab_view("OTC Screen", load_otc_screen, screen_list),
            tv_view,
            bnd_view,
        ]),
    ]))

    log_panel = ft.Container(
        content=log_view, height=200, padding=8,
        border=ft.Border.all(1, ft.Colors.GREY), border_radius=6)

    back_row = ft.Row([
        ft.TextButton(content=ft.Text("← Назад"),
                      on_click=lambda _: page.run_task(on_back)),
        ft.Text("Cookies — OTC / Screen / TradingView / Binodex", italic=True,
                color=ft.Colors.GREY),
    ], spacing=12)

    page.add(ft.Column([
        back_row,
        tabs,
        ft.Row([ft.TextButton(content=ft.Text("🧹 Очистить лог"),
                              on_click=lambda _: sink.clear())],
               alignment=ft.MainAxisAlignment.END),
        log_panel,
    ], expand=True))

    # стартовая загрузка списков
    await load_otc_option()
    await load_otc_screen()
    await load_tv_old()
    await load_binodex_old()
    print("✅ Готово к работе")

    async def teardown():
        """Выход с экрана: вернуть stdout, закрыть пулы БД и браузер, снять свой clipboard."""
        sys.stdout = old_stdout
        try:
            await db.close()
        except (Exception,):
            pass
        try:
            await browser.shutdown()
        except (Exception,):
            pass
        if added_clipboard:
            try:
                page.services.remove(clipboard)
            except (Exception,):
                pass

    return teardown


async def _noop():
    return None


async def main(page: ft.Page):
    """Standalone-запуск (отладка вне DeployManager): окно + экран без «Назад»."""
    page.title = "CookiesProgram2"
    try:
        page.window.width, page.window.height = 1100, 820
    except (Exception,):
        pass
    await build_screen(page, on_back=_noop)
