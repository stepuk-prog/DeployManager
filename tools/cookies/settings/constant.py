"""Жёсткие константы и данные (НЕ из БД).

Поля логина pocketoption, маппинг логических селекторов → par_name в
settings.pocket_settings, шаги настройки сайта binodex и параметры Privy-почты.
"""

# ----- Поля логина pocketoption (классы контейнеров на странице авторизации) -----
MAIL_FIELD = "email-container"
PASSWORD_FIELD = "password-container"
BUTTON_FIELD = "submit-btn-wrap"

# ----- Логический ключ селектора → par_name в Program.settings.pocket_settings -----
POCKET_PARAM_NAMES = {
    "otc_val_list_close": "list_close_header",
    "trade_window": "trade_window",
    "timeframe_otc": "timeframe",
    "change_tf": "change_tf",
    "chart_type": "chart_type",
    "s30": "s30_css",
}

# Вход на binodex (email-OTP) констант здесь БОЛЬШЕ НЕ ДЕРЖИТ: отправители письма, подстрока
# темы, ключи сессии и обязательные селекторы живут в `settings.binodex_settings` и читаются
# ядром (`binocore.binodex`). Прежние PRIVY_* знали только Privy и потому слепли на аккаунте с
# собственной авторизацией binodex — см. докстринг apps/binodex.py.

# Шаги настройки сайта binodex (par_name «открыть» → «выбрать»). После всех — повторный
# клик по setup_settings_open закрывает окно. Настройки персистят за аккаунтом.
SETUP_STEPS = [
    ("setup_candle_scale", "setup_candle_scale_item"),
    ("setup_chart_scale",  "setup_chart_scale_item"),
]
