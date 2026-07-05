"""Ядро логина юзербота: ручной флоу авторизации Pyrogram (pyrofork) → session_string.

Зачем не `app.start()`: тот зовёт `input()` для телефона/кода/пароля — несовместимо с
GUI (нет терминала) и с нашим единым UI-слоем. Поэтому низкоуровневый флоу:
    Client(in_memory=True) → connect() → send_code(phone) → sign_in(phone, hash, code)
    → [SessionPasswordNeeded → check_password(pwd)] → get_me() → export_session_string()
Код входа приходит в Telegram аккаунта (или SMS) — оператор вводит его вручную через
`ui.ask`; туда же при облачном пароле 2FA. На почту Telegram-коды НЕ приходят, авто-режима нет.

Сессия экспортируется в строку (in_memory, без .session-файла) и валидна с любого IP —
логин идёт с машины оператора, как в BinoOptions/scripts/reauth_userbot.py.

Все СЕТЕВЫЕ вызовы Telegram обёрнуты тайм-аутом (`_rpc`), чтобы не зависать на сетевом
стопоре; запросы к оператору (`ui.ask`) тайм-аутом НЕ ограничиваем (ждём человека).
"""
import asyncio
import base64
import re
import socket
import struct

from pyrogram import Client
from pyrogram.errors import (ApiIdInvalid, FloodWait, PasswordHashInvalid, PhoneCodeExpired,
                             PhoneCodeInvalid, PhoneNumberInvalid, SessionPasswordNeeded)
from pyrogram.session.internals import DataCenter

from core import ui

# Тайм-аут на один сетевой вызов Telegram (сек) — не зависать.
_RPC_TIMEOUT = 60


class SessionError(Exception):
    """Невосстановимая ошибка логина (неверный телефон, FloodWait, таймаут сети)."""


class ApiCredentialsError(SessionError):
    """api_id/api_hash неверны или не совпадают между собой (Telegram: API_ID_INVALID).
    Отдельный тип — чтобы caller мог переспросить пару и повторить, а не падать."""


def _client_name(phone: str) -> str:
    """Имя клиента Pyrogram (для in_memory не пишется на диск, но обязано быть валидным)."""
    return "sm_" + re.sub(r"\D", "", phone or "userbot")


async def _rpc(coro, what: str):
    """Сетевой вызов Telegram под тайм-аутом → `SessionError` вместо вечного зависания.
    Прочие исключения Pyrogram (PhoneCodeInvalid/SessionPasswordNeeded и т.п.) пробрасываются."""
    try:
        return await asyncio.wait_for(coro, timeout=_RPC_TIMEOUT)
    except asyncio.TimeoutError:
        raise SessionError(f"Таймаут Telegram на «{what}» ({_RPC_TIMEOUT} с) — повтори позже")


async def _send_code(app: Client, phone: str):
    """Запросить код (первичная отправка И повтор при PhoneCodeExpired) — с тайм-аутом и
    понятными ошибками вместо сырых исключений. Едина для обоих мест отправки."""
    try:
        return await _rpc(app.send_code(phone), "отправку кода")
    except ApiIdInvalid:
        raise ApiCredentialsError(
            "api_id/api_hash неверны или не совпадают между собой "
            "(проверь пару на my.telegram.org; частая причина — лишний пробел в api_hash)")
    except PhoneNumberInvalid:
        raise SessionError(f"Неверный номер телефона: {phone!r}")
    except FloodWait as e:
        raise SessionError(f"FloodWait: Telegram просит подождать {e.value} с — повтори позже")


def _telethon_string(dc_id: int, ip: str, port: int, auth_key: bytes) -> str:
    """Собрать Telethon StringSession из общего auth_key/dc (та же сессия Telegram, другой
    «конверт»). Формат: "1" + base64url(struct ">B 4s H 256s": dc_id·ip·port·auth_key).
    IPv4 → 4 байта (inet_aton); IP/порт DC берём из таблицы pyrofork (DataCenter)."""
    packed = struct.pack(">B4sH256s", dc_id, socket.inet_aton(ip), port, auth_key)
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


async def export_session(api_id: int, api_hash: str, phone: str, telethon: bool = False):
    """Залогинить юзербота и вернуть (me, session_string) либо None при отмене оператором.

    telethon=True — вернуть строку в формате Telethon StringSession (тот же auth_key/dc,
    что у Pyrogram, просто иной формат); иначе — родная Pyrogram-строка. Хранилище одно
    (колонка session_string) — Telethon применяется крайне редко.
    Бросает SessionError на невосстановимых ошибках (неверный телефон/FloodWait/таймаут),
    ApiCredentialsError — при неверной паре api_id/api_hash (caller переспрашивает).
    Код подтверждения и пароль 2FA запрашиваются через core.ui (в GUI — диалоги).
    """
    phone = (phone or "").strip()
    app = Client(name=_client_name(phone), api_id=api_id, api_hash=api_hash, in_memory=True)
    try:
        await _rpc(app.connect(), "подключение к Telegram")
    except SessionError:
        raise
    except (Exception,) as error:
        raise SessionError(f"Не удалось подключиться к Telegram: {error}")
    try:
        # 1) запрос кода (FloodWait/неверный номер/неверная пара api — внутри _send_code)
        sent = await _send_code(app, phone)
        print(f"📨 Код отправлен в Telegram для {phone} (тип: {getattr(sent, 'type', '—')})")

        # 2) ввод кода + при необходимости пароль 2FA
        me = None
        while me is None:
            code = await ui.ask(
                f"Код подтверждения из Telegram для {phone}", cancelable=True)
            if code is None:
                print("✖️ Отменено оператором.")
                return None
            code = code.strip().replace(" ", "")
            try:
                result = await _rpc(app.sign_in(phone, sent.phone_code_hash, code), "вход по коду")
                # sign_in вернул User → авторизованы; иное (TermsOfService/bool) → добиваем get_me
                me = result if hasattr(result, "id") else await _rpc(app.get_me(), "get_me")
            except PhoneCodeInvalid:
                print("❌ Неверный код, попробуй ещё раз.")
            except PhoneCodeExpired:
                print("⚠️ Код истёк — запрашиваю новый.")
                sent = await _send_code(app, phone)
            except SessionPasswordNeeded:
                me = await _check_2fa(app)
                if me is None:           # отмена на этапе пароля
                    return None
            except FloodWait as e:
                raise SessionError(f"FloodWait: подождать {e.value} с — повтори позже")

        if telethon:
            # storage — локальная in_memory-память (не сеть), тайм-аут не нужен
            dc_id = await app.storage.dc_id()
            test_mode = await app.storage.test_mode()
            auth_key = await app.storage.auth_key()
            ip, port = DataCenter(dc_id, test_mode, False, False, False)
            session_string = _telethon_string(dc_id, ip, port, auth_key)
        else:
            session_string = await _rpc(app.export_session_string(), "экспорт сессии")
        fmt = "Telethon" if telethon else "Pyrogram"
        uname = f"@{me.username}" if getattr(me, "username", None) else "—"
        print(f"✅ Логин ОК [{fmt}]: {me.first_name or '—'} (id={me.id}, {uname}, "
              f"premium={bool(getattr(me, 'is_premium', False))})")
        return me, session_string
    finally:
        try:
            await app.disconnect()
        except (Exception,):
            pass


async def _check_2fa(app: Client):
    """Облачный пароль 2FA: цикл ввода до успеха/отмены. Возвращает me или None (отмена)."""
    print("🔐 Включён облачный пароль (2FA) — введи его.")
    while True:
        pwd = await ui.ask("Облачный пароль (2FA)", cancelable=True)
        if pwd is None:
            print("✖️ Отменено оператором.")
            return None
        try:
            return await _rpc(app.check_password(pwd), "проверку пароля 2FA")
        except PasswordHashInvalid:
            print("❌ Неверный пароль 2FA, попробуй ещё раз.")
        except FloodWait as e:
            raise SessionError(f"FloodWait: подождать {e.value} с — повтори позже")
