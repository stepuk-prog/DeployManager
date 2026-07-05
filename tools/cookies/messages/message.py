"""Тексты уведомлений (Telegram) о созданных cookies."""


def otc_cookies_saved(name: str, mail: str) -> str:
    return f"✅ OTC cookies сохранены\nАккаунт: {name}\nПочта: {mail}"


def tv_cookies_saved(name: str, mail: str) -> str:
    return f"✅ TradingView cookies сохранены\nАккаунт: {name}\nПочта: {mail}"


def binodex_cookies_saved(name: str, mail: str, mode: str) -> str:
    return f"✅ Binodex cookies сохранены ({mode})\nАккаунт: {name}\nПочта: {mail}"
