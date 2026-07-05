"""Конфигурация CookiesProgram2 из .env.

Env → PG (два пула), Telegram, опции браузера. Тут же — URL и LAUNCH/CONTEXT
опции Playwright (Firefox): отдельный профиль для OTC/TV и для binodex (headful,
для ручного вмешательства). Жёсткие константы/данные (поля логина, селекторы-маппинг,
шаги настройки, Privy) — в settings/constant.py.
"""
import os

from dotenv import load_dotenv

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ----- PostgreSQL (через PgBouncer, transaction mode) -----
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "6442"))
PG_USER = os.getenv("PG_USER")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_DB_PROGRAM = os.getenv("PG_DB_PROGRAM", "Program")
PG_DB_BINODEX = os.getenv("PG_DB_BINODEX", "binodex")
# Ключи пулов → имена БД (для database/db.py). 'program' — почта/cookies pocket+tv,
# 'binodex' — селекторы + storage_state.
DB_NAMES = {"program": PG_DB_PROGRAM, "binodex": PG_DB_BINODEX}

# ----- Telegram (уведомления о созданных cookies) -----
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHANNEL = os.getenv("TG_CHANNEL", "")

# ----- Браузер -----
# *_HEADLESS=0 (дефолт) — видимое окно (нужно для ручного вмешательства в binodex
# и для контроля OTC/TV-логина).
_OTC_HEADLESS = os.getenv("OTC_HEADLESS", "0") != "0"
_BINODEX_HEADLESS = os.getenv("BINODEX_HEADLESS", "0") != "0"
BINODEX_VW = int(os.getenv("BINODEX_VW", "1280"))
BINODEX_VH = int(os.getenv("BINODEX_VH", "800"))

# ----- URL -----
OTC_URL = "https://pocketoption.com/ru/cabinet/quick-high-low/"
TV_URL = "https://ru.tradingview.com/"
BINODEX_LANDING = "https://binodex.app/"
BINODEX_TRADE = "https://app.binodex.app/trade"

# ----- Playwright (Firefox) — OTC / TradingView (порт browser_setting.py) -----
_OTC_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:118.0) Gecko/20100101 Firefox/118.0"
OTC_LAUNCH_OPTIONS = {
    "headless": _OTC_HEADLESS,
    "firefox_user_prefs": {
        "general.useragent.override": _OTC_UA,
        "browser.startup.homepage": "about:blank",
        "startup.homepage_welcome_url": "",
        "startup.homepage_welcome_url.additional": "",
        "browser.aboutConfig.showWarning": False,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "datareporting.healthreport.uploadEnabled": False,
        "browser.newtabpage.enabled": False,
        "browser.newtab.preload": False,
        "browser.tabs.warnOnClose": False,
        "signon.rememberSignons": False,
        "dom.webdriver.enabled": False,
        "useAutomationExtension": False,
        "media.volume_scale": "0.0",
        "ui.systemUsesDarkTheme": 1,
    },
}
OTC_CONTEXT_OPTIONS = {
    "user_agent": _OTC_UA,
    "viewport": {"width": 1687, "height": 901},
    "color_scheme": "dark",
}

# ----- Playwright (Firefox) — binodex (headful для ручного вмешательства) -----
_BINODEX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0"
BINODEX_LAUNCH_OPTIONS = {
    "headless": _BINODEX_HEADLESS,
    "firefox_user_prefs": {
        "general.useragent.override": _BINODEX_UA,
        "dom.webdriver.enabled": False,
        "media.volume_scale": "0.0",       # mute (на нагрузку сайта не влияет)
        "media.autoplay.default": 5,
        "toolkit.telemetry.enabled": False,
        "datareporting.healthreport.uploadEnabled": False,
        "ui.systemUsesDarkTheme": 1,
    },
}
BINODEX_CONTEXT_OPTIONS = {
    "user_agent": _BINODEX_UA,
    "viewport": {"width": BINODEX_VW, "height": BINODEX_VH},
    "color_scheme": "dark",
}
