"""Telegram-операции над аккаунтами персонажей: живость сессии и настройки приватности.

Отдельно от `session.py`: там ЛОГИН (код, 2FA, экспорт строки), здесь — работа готовой
сессией без ввода кода. Ни один метод ничего не отправляет в чаты: только чтение профиля
и настроек приватности, плюс их правка по явной команде оператора.

Темп: пауза между аккаунтами задаётся вызывающим (`personas.py`). Telegram считает
подозрительным не чтение, а поток — но 30+ подключений подряд с одного IP всё равно
лучше разносить.
"""
import asyncio

from pyrogram import Client
from pyrogram.raw import functions, types

# Норма приватности для персонажа и причина каждой строки.
#   номер телефона  — «+62» на русском форуме выдаёт происхождение первым же кликом;
#   звонки          — звонок от незнакомца персонажу не нужен, а репорт с него — риск;
#   добавление в чаты — иначе аккаунт затащат в спам-чаты, а за них Telegram ограничивает;
#   время захода    — круглосуточный онлайн у «человека» читается как машина;
#   пересылки/фото  — как у обычного участника, скрывать нечего.
PRIVACY_NORM = {
    "номер телефона":    (types.InputPrivacyKeyPhoneNumber,     "nobody"),
    "звонки":            (types.InputPrivacyKeyPhoneCall,       "nobody"),
    "добавление в чаты": (types.InputPrivacyKeyChatInvite,      "contacts"),
    "время захода":      (types.InputPrivacyKeyStatusTimestamp, "contacts"),
    "пересылки":         (types.InputPrivacyKeyForwards,        "everybody"),
    "фото профиля":      (types.InputPrivacyKeyProfilePhoto,    "everybody"),
}
_RULES = {
    "nobody":    [types.InputPrivacyValueDisallowAll()],
    "contacts":  [types.InputPrivacyValueAllowContacts()],
    "everybody": [types.InputPrivacyValueAllowAll()],
}
_RU = {"nobody": "никому", "contacts": "контакты", "everybody": "все"}
_VALUE_RU = {
    "PrivacyValueAllowAll": "все", "PrivacyValueDisallowAll": "никому",
    "PrivacyValueAllowContacts": "контакты", "PrivacyValueDisallowContacts": "кроме контактов",
    "PrivacyValueAllowUsers": "отдельные люди", "PrivacyValueDisallowUsers": "кроме отдельных",
    "PrivacyValueAllowCloseFriends": "близкие",
}


def _client(row) -> Client:
    """Клиент на готовой строке сессии, без файла на диске.

    ⚠️ Отпечаток берём ИЗ РЕЕСТРА (`device_model`/`system_version`/`app_version`/`lang_code`).
    По умолчанию pyrofork представляется как «CPython 3.11.9» — и тогда все наши аккаунты
    выглядят для Telegram одним и тем же скриптом, а разнесённые по строкам отпечатки не
    работают вовсе. Пустые поля оставляем на усмотрение библиотеки: подставлять один
    выдуманный дефолт на всех — та же болезнь."""
    extra = {}
    if row["device_model"]:
        extra["device_model"] = row["device_model"]
    if row["system_version"]:
        extra["system_version"] = row["system_version"]
    if row["app_version"]:
        extra["app_version"] = row["app_version"]
    if row["lang_code"]:
        extra["lang_code"] = row["lang_code"]
    return Client(name=f"person{row['id']}", api_id=row["api_id"], api_hash=row["api_hash"],
                  session_string=row["session_string"], in_memory=True, **extra)


async def check_alive(row) -> tuple[bool, str, dict | None]:
    """Жива ли сессия: один `get_me()`. Возвращает (жива, сообщение, профиль|None).

    Сессия может быть разлогинена на стороне Telegram (AUTH_KEY_UNREGISTERED) — это и есть
    основная причина, по которой персонаж молча перестаёт говорить. Ошибку отдаём текстом:
    caller кладёт её в `person.account.note`, чтобы было видно, что именно случилось."""
    if not row["session_string"]:
        return False, "нет session_string", None
    try:
        async with _client(row) as app:
            me = await app.get_me()
        return True, "ok", {"user_id": me.id, "first_name": me.first_name or "",
                            "last_name": me.last_name or "", "username": me.username or ""}
    except (Exception,) as error:                                    # noqa: BLE001
        return False, f"{type(error).__name__}: {error}", None


def _human(rules) -> str:
    """Ответ Telegram о правиле — словами оператора."""
    return ", ".join(_VALUE_RU.get(type(r).__name__, type(r).__name__) for r in rules) or "—"


async def privacy(row, harden: bool = False) -> list[tuple[str, str, bool]]:
    """Настройки приватности аккаунта: [(что, как сейчас, соответствует ли норме)].

    harden=True — несоответствия сразу приводятся к норме (по одному SetPrivacy на пункт).
    Читаем ВСЕГДА перед записью: иначе не видно, что именно поменяли, а «выставил на всякий
    случай» затирает осмысленную ручную настройку."""
    out: list[tuple[str, str, bool]] = []
    async with _client(row) as app:
        for label, (key, want) in PRIVACY_NORM.items():
            cur = await app.invoke(functions.account.GetPrivacy(key=key()))
            now = _human(cur.rules)
            ok = now == _RU[want]
            if harden and not ok:
                await app.invoke(functions.account.SetPrivacy(key=key(), rules=_RULES[want]))
                now = f"{now} → {_RU[want]}"
                ok = True
            out.append((label, now, ok))
            await asyncio.sleep(0.4)
    return out

async def get_profile(row) -> dict:
    """Текущий профиль аккаунта: имя, @username, био, есть ли фото.

    Био и фото одним `get_chat("me")` — `get_me()` их не отдаёт."""
    async with _client(row) as app:
        me = await app.get_me()
        full = await app.get_chat("me")
        return {"user_id": me.id, "first_name": me.first_name or "",
                "last_name": me.last_name or "", "username": me.username or "",
                "bio": getattr(full, "bio", "") or "",
                "has_photo": bool(getattr(full, "photo", None))}


async def set_profile(row, first_name: str | None = None, last_name: str | None = None,
                      bio: str | None = None, username: str | None = None,
                      photo_path: str | None = None) -> list[str]:
    """Сменить профиль персонажа. Возвращает список сделанного (для лога оператора).

    ⚠️ Смена имени — заметное действие: Telegram хранит историю и часть клиентов её
    показывает. Персонаж, переименовывающийся раз в неделю, выглядит хуже, чем с чужим
    именем — потому имя ставится ОДИН раз и осознанно, вызов идёт только по явно
    переданным полям (None = не трогать).

    username занимают глобально: занятый отдаётся ошибкой Telegram, её пробрасываем —
    caller спросит другой."""
    done: list[str] = []
    async with _client(row) as app:
        if first_name is not None or last_name is not None or bio is not None:
            await app.update_profile(
                first_name=first_name if first_name is not None else None,
                last_name=last_name if last_name is not None else None,
                bio=bio if bio is not None else None)
            if first_name is not None:
                done.append(f"имя → «{first_name}»")
            if last_name is not None:
                done.append(f"фамилия → «{last_name or '(убрана)'}»")
            if bio is not None:
                done.append(f"био → «{bio or '(пусто)'}»")
        if username is not None:
            await app.set_username(username or None)
            done.append(f"username → @{username}" if username else "username убран")
        if photo_path:
            await app.set_profile_photo(photo=photo_path)
            done.append("фото профиля поставлено")
    return done
