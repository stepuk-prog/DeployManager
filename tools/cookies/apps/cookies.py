"""Нормализация cookies Playwright → форма для Selenium-потребителей. Порт
_normalize_cookies из старого apps/app.py.

ВАЖНО: cookies.pocket_cookies / cookies.tv_cookies — jsonb, а json-codec на пуле сам
сериализует. Поэтому возвращаем list[dict], а НЕ json-строку (иначе двойное кодирование).
"""


def _domain_matches(cookie: dict, suffix: str) -> bool:
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    return domain == suffix or domain.endswith("." + suffix)


def normalize_cookies(cookies: list[dict], domain_suffix: str) -> list[dict]:
    """1. Оставить только домен `domain_suffix` и его поддомены (Playwright отдаёт всю
       банку — трекеры/реклама/recaptcha; Selenium-потребитель не сможет add_cookie на
       чужой домен → InvalidCookieDomainException).
    2. `expires` → `expiry` (int): потребитель кладёт dict прямо в driver.add_cookie,
       Marionette отвергает неизвестное поле `expires`. `expires == -1` (session) —
       удаляем оба поля (Playwright-потребители переживут отсутствие).
    """
    normalized: list[dict] = []
    for cookie in cookies:
        if not _domain_matches(cookie, domain_suffix):
            continue
        item = dict(cookie)
        exp = item.pop("expires", None)
        if exp is not None and exp != -1:
            item["expiry"] = int(exp)
        normalized.append(item)
    return normalized
