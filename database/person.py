"""Доступ к БД **forum**, схема `person` — юзерботы ИИ-персонажей (проект ForumPersonas).

ПОЧЕМУ ОТДЕЛЬНЫЙ КЛАСС, А НЕ МЕТОДЫ В `Database`. Это ДРУГАЯ база: юзерботы флота живут
в `Program.telegram.telegram` (у каждого своя программа, свой Desktop из справочника
`telegram.telegram_apps`), а юзерботы персонажей — в `forum.person.account` (свои api-креды,
две сессии на аккаунт, слоты Desktop прямо в строке). Сджойнить их запросом нельзя, и
смешивать методы в одном классе значит однажды записать сессию не в ту базу.

Машинерия пула, ретраев и контракт ошибок наследуются от `Database` — здесь только запросы.
"""
import asyncpg

from database.db import Database
from settings import config


class PersonDatabase(Database):
    """БД forum: аккаунты персонажей (`person.account`) и сами персонажи (`person.persona`)."""

    def __init__(self, min_size: int = 1, max_size: int = 3):
        super().__init__(min_size=min_size, max_size=max_size, database=config.PG_DB_FORUM)

    # ----- аккаунты -----
    async def list_accounts(self, only_slotted: bool = False) -> list[asyncpg.Record]:
        """Карта аккаунтов: слот Desktop, профиль, статус и персонаж, если назначен.
        only_slotted=True — только разложенные по Desktop-слотам (резерв не показываем)."""
        return await self._query(
            "SELECT a.id, a.slot, a.slot_pos, a.phone, a.user_id, a.username, "
            "trim(coalesce(a.first_name,'') || ' ' || coalesce(a.last_name,'')) AS full_name, "
            "a.status, a.cohort, a.note, "
            "(a.session_string IS NOT NULL) AS has_session, "
            "(a.session_telethon IS NOT NULL) AS has_telethon, "
            "(a.twofa_password IS NOT NULL) AS has_2fa, "
            "p.key AS persona_key, p.role AS persona_role "
            "FROM person.account a "
            "LEFT JOIN person.persona p ON p.account_id = a.id "
            + ("WHERE a.slot IS NOT NULL " if only_slotted else "")
            + "ORDER BY a.slot NULLS LAST, a.slot_pos, a.phone",
            func="list_accounts",
        )

    async def get_account(self, ident: str) -> asyncpg.Record | None:
        """Полная строка аккаунта по телефону или user_id (нецифры в аргументе игнорируются).
        Отдаёт секреты (api_hash/session/2FA) — нужно для логина."""
        digits = "".join(c for c in ident if c.isdigit())
        return await self._query(
            "SELECT a.*, p.key AS persona_key FROM person.account a "
            "LEFT JOIN person.persona p ON p.account_id = a.id "
            "WHERE regexp_replace(a.phone,'[^0-9]','','g') = $1 OR a.user_id::text = $1",
            digits, mode="row", func="get_account",
        )

    async def save_session(self, account_id: int, session_string: str,
                           telethon: bool = False) -> str:
        """Записать свежую сессию. telethon=True — в `session_telethon`, иначе в
        `session_string` (pyrofork). Колонки РАЗНЫЕ: строки несовместимы по формату,
        и перезапись одной другой оставила бы аккаунт без рабочей сессии."""
        column = "session_telethon" if telethon else "session_string"
        return await self._query(
            f"UPDATE person.account SET {column} = $1, status = 'ready', "
            f"note = CASE WHEN note LIKE 'getMe:%' THEN NULL ELSE note END, "
            f"updated_at = now() WHERE id = $2",
            session_string, account_id, mode="execute", func="save_session",
        )

    async def set_status(self, account_id: int, status: str, note: str | None = None) -> str:
        """Статус аккаунта: new/ready/warming/in_use/limited/dead (+ заметка о причине)."""
        return await self._query(
            "UPDATE person.account SET status = $1, "
            "note = coalesce($2, note), updated_at = now() WHERE id = $3",
            status, note, account_id, mode="execute", func="set_status",
        )

    async def update_profile(self, account_id: int, user_id: int, first_name: str,
                             last_name: str, username: str | None) -> str:
        """Профиль из Telegram (getMe) — в реестр. Заодно чистим протухшую getMe-заметку."""
        return await self._query(
            "UPDATE person.account SET user_id = $1, first_name = $2, last_name = $3, "
            # 'ready' ставим только тем, кто числился мёртвым или новым: аккаунт, уже
            # занятый персонажем (in_use), синхронизация профиля разжаловать не должна.
            "username = $4, status = CASE WHEN status IN ('dead','new') THEN 'ready' "
            "                            ELSE status END, "
            "note = CASE WHEN note LIKE 'getMe:%' THEN NULL ELSE note END, "
            "updated_at = now() WHERE id = $5",
            user_id, first_name, last_name, username or None, account_id,
            mode="execute", func="update_profile",
        )

    async def mark_dead(self, account_id: int, reason: str) -> str:
        """Сессия не поднялась — гасим аккаунт и пишем причину. Персонаж на нём молчит,
        остальные работают (деградация по персонажу, а не смерть программы)."""
        return await self._query(
            "UPDATE person.account SET status = 'dead', note = $1, updated_at = now() "
            "WHERE id = $2",
            f"getMe: {reason}"[:500], account_id, mode="execute", func="mark_dead",
        )

    # ----- персонажи -----
    async def list_personas(self) -> list[asyncpg.Record]:
        """Персонажи с их аккаунтом и ступенями черт (человекочитаемая карточка)."""
        return await self._query(
            "SELECT c.id, c.key, c.role, c.ник AS nick, c.телефон AS phone, "
            "c.десктоп AS slot, c.включён AS enabled, c.премодерация AS premoderate, "
            "c.черты AS traits, a.status AS account_status "
            "FROM person.v_persona_card c "
            "LEFT JOIN person.account a ON a.phone = c.телефон "
            "ORDER BY c.id",
            func="list_personas",
        )
