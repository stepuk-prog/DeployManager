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

# ----- Privy (binodex email-OTP) -----
# Код приходит с ДВУХ адресов (no-reply@privy.io И no-reply@mail.privy.io) — фильтр по
# домену (подстрока в FROM матчит оба).
PRIVY_FROM = "privy.io"
PRIVY_SUBJECT_HINT = "login code"   # тема: "Your login code for BinoDex"
PRIVY_CODE_WAIT_SECONDS = 120
PRIVY_CODE_POLL_EVERY = 3

# Обязательные селекторы логина binodex — без них вход невозможен (падаем понятно).
REQUIRED_LOGIN_SELECTORS = ("login_open", "login_email", "login_submit", "login_code_inputs")

# Шаги настройки сайта binodex (par_name «открыть» → «выбрать»). После всех — повторный
# клик по setup_settings_open закрывает окно. Настройки персистят за аккаунтом.
SETUP_STEPS = [
    ("setup_candle_scale", "setup_candle_scale_item"),
    ("setup_chart_scale",  "setup_chart_scale_item"),
]
