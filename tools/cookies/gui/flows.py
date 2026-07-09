"""Оркестрация флоу по вкладкам: связывает БД + браузер + визард + уведомления.

Каждый флоу ведёт один аккаунт через WizardDialog (видимый браузер). Браузерные примитивы —
в apps/otc.py и apps/binodex.py; тут — последовательность шагов, точки выбора и сохранение.
Правило #6: на внешних вызовах таймауты (в примитивах). Правило #8: best-effort cleanup тихо,
прочие сбои — в лог + статус визарда.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable

import flet as ft

from tools.cookies.apps import binodex as bnd
from tools.cookies.apps import imap_code, otc
from tools.cookies.apps.cookies import normalize_cookies
from tools.cookies.apps.notify import notify
from tools.cookies.classes import BrowserManager
from tools.cookies.database import Database
from tools.cookies.gui.backend import FletUi
from tools.cookies.gui.wizard import WizardDialog
from tools.cookies.logs import init_logger
from tools.cookies.messages import message
from tools.cookies.settings import config
from tools.cookies.settings.pocket import resolve_selectors

logger = init_logger(__name__)


@dataclass
class FlowContext:
    page: ft.Page
    ui: FletUi
    db: Database
    browser: BrowserManager
    copy: Callable[[str], None]   # копировать текст в буфер обмена


# ----------------------------- общие хелперы UI -----------------------------

def _copy_row(ctx: FlowContext, label: str, value: str) -> ft.Row:
    return ft.Row([
        ft.Text(f"{label}: {value}", selectable=True, expand=True),
        ft.IconButton(icon=ft.Icons.CONTENT_COPY, tooltip="Копировать",
                      on_click=lambda e, v=value: ctx.copy(v)),
    ])


def _info_rows(ctx: FlowContext, name: str, mail: str, password: str,
               pass_label: str = "Пароль") -> list[ft.Control]:
    return [
        ft.Text(f"Аккаунт: {name}", weight=ft.FontWeight.BOLD),
        _copy_row(ctx, "Почта", mail or "—"),
        _copy_row(ctx, pass_label, password or "—"),
    ]


# ----------------------------- OTC Option / OTC Screen -----------------------------

async def otc_flow(ctx: FlowContext, account: dict, title_prefix: str) -> None:
    """OTC Option и OTC Screen (одинаковый флоу, обе пишут в cookies.pocket_cookies)."""
    acc = dict(account)
    name = acc.get("program_name") or acc.get("name") or f"User {acc.get('cookies_pocket')}"
    mail = acc.get("mail", "")
    user_id = acc.get("cookies_pocket")

    wiz = WizardDialog(ctx.page, f"{title_prefix}: {name}",
                       info_rows=_info_rows(ctx, name, mail, acc.get("pocket_pass", "")))
    wiz.open()
    if await wiz.choose([("Создать Cookies", "create", "ok"),
                         ("Отмена", "cancel", "no")]) == "cancel":
        wiz.close()
        return

    sel_rows = await ctx.db.pocket_settings()
    if sel_rows is False:
        wiz.status("❌ Ошибка чтения настроек pocket из БД")
        await wiz.choose([("Закрыть", "close", "no")])
        wiz.close()
        return
    sel = resolve_selectors(sel_rows)

    wiz.busy(True)
    wiz.status("Запуск браузера…")
    session = await ctx.browser.launch(config.OTC_LAUNCH_OPTIONS, config.OTC_CONTEXT_OPTIONS)
    try:
        wiz.status(f"Открываю {config.OTC_URL}")
        ok, msg = await otc.navigate(session.page, config.OTC_URL)
        if not ok:
            wiz.status(f"❌ {msg}")
            await wiz.choose([("Закрыть", "close", "no")])
            return
        wiz.status("Авторизация…")
        ok, msg = await otc.full_auth_data(session.page, acc)
        if not ok:
            wiz.status(f"❌ {msg}")
            await wiz.choose([("Закрыть", "close", "no")])
            return

        choice = await wiz.choose([("Настроить авто", "auto", "ok"),
                                   ("Настроить вручную", "manual", "neutral"),
                                   ("Закрыть", "close", "no")])
        if choice == "close":
            return
        if choice == "auto":
            wiz.busy(True)
            wiz.status("Авто-настройка графика…")
            ok, msg = await otc.init_otc(session.page, sel)
            wiz.status("✅ Авто-настройка завершена" if ok else f"⚠️ {msg}")

        if await wiz.choose([("Сохранить Cookies", "save", "ok"),
                             ("Закрыть без сохранения", "close", "no")]) == "close":
            return

        wiz.busy(True)
        wiz.status("Сохраняю cookies…")
        raw = await session.context.cookies()
        normalized = normalize_cookies(raw, "pocketoption.com")
        saved = await ctx.db.save_pocket_cook(user_id=user_id, cookies=normalized)
        if saved is True:
            wiz.status(f"✅ Сохранено ({len(normalized)} cookies)")
            await notify(message.otc_cookies_saved(name, mail))
        else:
            wiz.status("❌ Ошибка сохранения в БД")
            await wiz.choose([("Закрыть", "close", "no")])
    finally:
        await session.close()
        wiz.close()


# ----------------------------- TradingView -----------------------------

async def tv_flow(ctx: FlowContext, account: dict, on_saved=None) -> None:
    """TradingView: открыть, закрыть google one-tap; оператор логинится вручную → сохранить.
    on_saved — async-колбэк, вызывается после успешного сохранения (в режиме «Добавить новый»
    перестраивает список, чтобы убрать аккаунт, у которого теперь есть строка в tv_cookies)."""
    acc = dict(account)
    user_id = acc.get("user_id")
    name = acc.get("google_account_name") or f"User {user_id}"
    mail = acc.get("mail", "")

    wiz = WizardDialog(ctx.page, f"TradingView: {name}",
                       info_rows=_info_rows(ctx, name, mail, acc.get("tv_pass", "")))
    wiz.open()
    if await wiz.choose([("Создать Cookies", "create", "ok"),
                         ("Отмена", "cancel", "no")]) == "cancel":
        wiz.close()
        return

    saved_ok = False
    wiz.busy(True)
    wiz.status("Запуск браузера…")
    session = await ctx.browser.launch(config.OTC_LAUNCH_OPTIONS, config.OTC_CONTEXT_OPTIONS)
    try:
        wiz.status(f"Открываю {config.TV_URL}")
        ok, msg = await otc.navigate(session.page, config.TV_URL)
        if not ok:
            wiz.status(f"❌ {msg}")
            await wiz.choose([("Закрыть", "close", "no")])
            return
        await otc.cook_tv(session.page)
        wiz.status("Войдите в аккаунт Google вручную в окне браузера, затем «Сохранить Cookies».")

        if await wiz.choose([("Сохранить Cookies", "save", "ok"),
                             ("Закрыть без сохранения", "close", "no")]) == "close":
            return

        wiz.busy(True)
        wiz.status("Сохраняю cookies…")
        raw = await session.context.cookies()
        normalized = normalize_cookies(raw, "tradingview.com")
        saved = await ctx.db.save_tv_cook(user_id=user_id, cookies=normalized)
        if saved is True:
            wiz.status(f"✅ Сохранено ({len(normalized)} cookies)")
            await notify(message.tv_cookies_saved(name, mail))
            saved_ok = True
        else:
            wiz.status("❌ Ошибка сохранения в БД")
            await wiz.choose([("Закрыть", "close", "no")])
    finally:
        await session.close()
        wiz.close()
    # Аккаунт сохранён → перестроить список (в режиме «Добавить новый» он отфильтруется как уже
    # имеющий строку в tv_cookies — не предлагаем создавать его повторно).
    if saved_ok and on_saved is not None:
        await on_saved()


# ----------------------------- Binodex -----------------------------

def _fmt_err(e) -> str:
    """Читаемый текст ошибки. imaplib кидает с bytes-аргументом
    (b'[AUTHENTICATIONFAILED] Invalid credentials') — декодируем, иначе в статусе «b'...'»."""
    args = getattr(e, "args", None)
    if args and isinstance(args[0], (bytes, bytearray)):
        return args[0].decode("utf-8", "replace")
    return str(e)


async def _bust_stale_assets(page) -> None:
    """Обойти протухший Cloudflare-кэш binodex. Эдж отдаёт устаревший `/assets/app.js`
    (static-имя, cf-cache HIT ~сутки), который ссылается на уже удалённый локаль-чанк →
    тот 404-ит с MIME text/plain → Firefox блокирует ES-модуль (NS_ERROR_CORRUPTED_CONTENT)
    → SPA не бутстрапится: пустая страница, кнопки логина нет. Обычный браузер грузит из
    старого кэша, поэтому «в браузере открывается, в Playwright — нет».
    Добавляем cache-bust query к static-именованным entry-файлам → CF MISS → origin отдаёт
    свежий app.js с живыми чанками. Хэш-чанки иммутабельны (новый деплой = новый хэш) — не трогаем."""
    cb = str(int(time.time()))

    async def _cb(route):
        url = route.request.url
        sep = "&" if "?" in url else "?"
        try:
            await route.continue_(url=f"{url}{sep}_cb={cb}")
        except (Exception,):   # старая версия Playwright без override url — не ломаем загрузку
            await route.continue_()

    await page.route("**/assets/app.js", _cb)
    await page.route("**/assets/app.css", _cb)


async def _binodex_launch_options(db, use_proxy: bool, status) -> dict:
    """launch-опции binodex + :50100-HTTP-прокси из settings.proxy_data через локальный релей
    (порт BinoOptions apps/browser_app._proxy_launch_options). Firefox не умеет socks5-auth →
    релей инжектит Proxy-Authorization, браузеру отдаём его адрес без авторизации.
    use_proxy=False или сбой подбора/релея → базовые опции (direct-фолбэк, как в BinoOptions)."""
    base = config.BINODEX_LAUNCH_OPTIONS
    if not use_proxy:
        return base
    from tools.cookies.settings import proxy as proxy_mod
    from tools.cookies.settings.local_proxy import start_local_proxy
    if not proxy_mod.proxy_list:
        await proxy_mod.load_proxies_from_db(db)
    p = proxy_mod.get_unused_proxy()
    if not p:
        status("⚠️ Нет активных :50100-прокси (settings.proxy_data) — иду напрямую")
        return base
    opts = dict(base)   # shallow: добавляем только верхний ключ 'proxy' (firefox_user_prefs не трогаем)
    if p.login and p.password:
        # start_local_proxy синхронный (socket.connect до ~3.3с) → в тред, чтобы не блокировать loop.
        host, port = await asyncio.to_thread(start_local_proxy, p.ip, p.port, p.login, p.password)
        if not host:
            status(f"⚠️ Локальный релей для {p.ip} не поднялся — иду напрямую")
            return base
        opts["proxy"] = {"server": f"http://{host}:{port}"}
        status(f"🛡️ Через прокси {p.ip}:{p.port} (релей {host}:{port})")
    else:
        opts["proxy"] = {"server": f"http://{p.ip}:{p.port}"}
        status(f"🛡️ Через прокси {p.ip}:{p.port}")
    return opts


async def binodex_flow(ctx: FlowContext, account: dict, mode: str, on_saved=None) -> None:
    """Binodex (Privy email-OTP). mode: 'old' (Обновить старый, do_setup=False) /
    'new' (Добавить новый, do_setup=True). Браузер видимый: при сбое окно НЕ закрываем,
    оператор доделывает вручную и жмёт «Сохранить». on_saved — async-колбэк, вызывается
    после успешного сохранения (в режиме 'new' перестраивает список, чтобы убрать аккаунт)."""
    acc = dict(account)
    user_id = acc["user_id"]
    name = acc.get("name") or f"User {user_id}"
    mail = acc.get("mail") or ""
    app_pass = acc.get("mail_app_pass") or ""
    do_setup = (mode == "new")
    mode_label = "Добавить новый" if do_setup else "Обновить старый"

    # «Обновить старый» даёт только user_id (=cookies_pocket) → почту/app-пароль дотягиваем
    # из telegram.telegram (cookies_pocket == id_telegram).
    if not mail or not app_pass:
        creds = await ctx.db.get_mail_creds(user_id)
        if creds and creds is not False:
            mail = mail or (creds["mail"] or "")
            app_pass = app_pass or (creds["mail_app_pass"] or "")

    wiz = WizardDialog(ctx.page, f"Binodex [{mode_label}]: {name}",
                       info_rows=_info_rows(ctx, name, mail, app_pass, pass_label="App-пароль"))
    wiz.open()
    entry = await wiz.choose([("Войти через прокси", "proxy", "ok"),
                              ("Войти напрямую", "direct", "neutral"),
                              ("Отмена", "cancel", "no")])
    if entry == "cancel":
        wiz.close()
        return
    use_proxy = (entry == "proxy")   # прокси из settings.proxy_data (как боевой binodex)

    if not mail or not app_pass:
        wiz.status("❌ Нет mail / mail_app_pass для аккаунта")
        await wiz.choose([("Закрыть", "close", "no")])
        wiz.close()
        return

    sel_rows = await ctx.db.binodex_selectors()
    if sel_rows is False:
        wiz.status("❌ Ошибка чтения селекторов binodex из БД")
        await wiz.choose([("Закрыть", "close", "no")])
        wiz.close()
        return
    sel = {r["par_name"]: r["par_value"] for r in sel_rows}
    missing = bnd.missing_login_selectors(sel)
    if missing:
        wiz.status(f"❌ Нет обязательных селекторов логина: {missing}")
        await wiz.choose([("Закрыть", "close", "no")])
        wiz.close()
        return

    session = None
    conn = None
    auto_ok = False

    # 1. IMAP-логин ОТДЕЛЬНО: если почта не пускает (неверный/протухший app-пароль) — браузер НЕ
    #    открываем (в браузере это не чинится), правится mail/mail_app_pass в telegram.telegram.
    try:
        wiz.busy(True)
        wiz.status("Подключаюсь к почте (IMAP)…")
        conn = await asyncio.to_thread(imap_code.imap_connect, mail, app_pass)
        baseline = set(await asyncio.to_thread(imap_code.privy_uids, conn))
    except (Exception,) as error:
        logger.warning("binodex_flow IMAP-логин (%s): %s", mail, error)
        wiz.status(f"❌ Почта отвергла вход: {_fmt_err(error)}")
        wiz.status("Проверь mail / mail_app_pass у аккаунта в telegram.telegram — нужен Gmail "
                   "app-пароль (16 симв.) при включённой 2FA и доступе IMAP. Браузер не открывал.")
        await wiz.choose([("Закрыть", "close", "no")])
        wiz.close()
        return

    # 2. Браузер + Privy: тут при сбое окно ОСТАВЛЯЕМ открытым (оператор доделывает вручную).
    try:
        wiz.status("Запуск браузера…")
        launch_opts = await _binodex_launch_options(ctx.db, use_proxy, wiz.status)
        session = await ctx.browser.launch(launch_opts, config.BINODEX_CONTEXT_OPTIONS)
        await _bust_stale_assets(session.page)   # обойти протухший CDN-кэш app.js (иначе пустая страница)
        wiz.status("Отправляю e-mail на binodex…")
        await bnd.open_and_send_email(session.page, sel, mail)

        wiz.status("Жду код Privy на почте…")
        code = await asyncio.to_thread(imap_code.wait_for_code, conn, baseline)
        wiz.status(f"Код получен: {code}")
        await bnd.enter_code(session.page, sel, code)

        wiz.status("Завершаю вход…")
        await bnd.finish_login(session.page)
        if do_setup:
            wiz.status("Прокликиваю настройку сайта…")
            await bnd.setup_site(session.page, sel)
        auto_ok = True
        wiz.status("✅ Вход выполнен автоматически")
    except (Exception,) as error:
        logger.warning("binodex_flow auto: %s", error)
        wiz.status(f"⚠️ Авто-флоу прервался: {_fmt_err(error)}")
        wiz.status("Окно браузера оставлено открытым — доделайте вход вручную и нажмите «Сохранить».")

    # Очистка писем Privy при успешном авто-входе (одноразовые коды).
    if auto_ok and conn is not None:
        try:
            removed = await asyncio.to_thread(imap_code.purge_privy, conn)
            if removed:
                wiz.status(f"Удалено писем Privy: {removed}")
        except (Exception,):
            pass
    if conn is not None:
        await asyncio.to_thread(imap_code.logout, conn)

    if session is None:
        # launch мог упасть уже ПОСЛЕ подъёма релея (session.close() не вызовется) — гасим сами.
        if use_proxy:
            from tools.cookies.settings.local_proxy import stop_local_proxy
            await asyncio.to_thread(stop_local_proxy)
        wiz.close()
        return

    # Точка сохранения (и для авто-успеха, и для ручного добивания).
    action = await wiz.choose([("Сохранить", "save", "ok"), ("Закрыть", "close", "no")])
    saved_ok = False
    if action == "save":
        wiz.busy(True)
        wiz.status("Снимаю storage_state и сохраняю…")
        try:
            state = await session.page.context.storage_state()
            saved = await ctx.db.save_binodex_state(user_id=user_id, storage_state=dict(state))
            if saved is True:
                wiz.status("✅ storage_state сохранён в БД")
                await notify(message.binodex_cookies_saved(name, mail, mode_label))
                saved_ok = True
            else:
                wiz.status("❌ Ошибка сохранения в БД")
                await wiz.choose([("Закрыть", "close", "no")])
        except (Exception,) as error:
            wiz.status(f"❌ Не удалось снять/сохранить storage_state: {error}")
            await wiz.choose([("Закрыть", "close", "no")])

    await session.close()
    wiz.close()
    # Аккаунт сохранён → перестроить список (в режиме 'new' он отфильтруется как уже
    # существующий в binodex_cookies — не предлагаем создавать его повторно).
    if saved_ok and on_saved is not None:
        await on_saved()
