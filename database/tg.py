"""Телеграм-домен БД Program (`telegram.telegram` + справочник клиентов).

Миксин к `Database` для суб-инструмента «Юзерботы (сессии)»: читаем/пишем креды
юзербота (api_id/api_hash/phone), session_string и почту; читаем справочник Telegram
Desktop клиентов (по `my_gram` знаем, какой клиент поднять при логине). Все методы
идут через общий `self._query` (машинерия пула/ретраев — в `database/db.py`).
Портировано из SessionManager/database/db.py.
"""
import asyncpg

from settings import config


class TelegramMixin:
    """Методы работы с `telegram.*`. Подмешивается в `Database` (даёт `self._query`)."""

    # ----- юзерботы (telegram.telegram) -----
    async def list_userbots(self, having_session: bool | None = None) -> list[asyncpg.Record]:
        """Все юзерботы со статусами (для списка/выбора). Без секретов в выборке —
        только факт наличия api_hash/session/почтового app-пароля.
        having_session: None — все; True — только с сессией (ветка «Обновить»);
        False — только БЕЗ сессии (session_string IS NULL, ветка «Создать»: сессия
        ещё не заводилась). Пустую строку '' не используем — её в данных нет.
        `programs` — имена программ, где юзербот числится в program.programdata.user_bot
        (int8[]); может быть несколько (string_agg) или NULL, если нигде не привязан."""
        if having_session is True:
            where = "WHERE t.session_string IS NOT NULL "
        elif having_session is False:
            where = "WHERE t.session_string IS NULL "
        else:
            where = ""
        # «Создать» (без сессии) — строго по алфавиту имён; иначе группируем по программе.
        order = "t.name" if having_session is False else "pr.programs NULLS LAST, t.name"
        return await self._query(
            "SELECT t.id_telegram, t.name, t.telegram_name, t.phone, t.api_id, "
            "(t.api_hash IS NOT NULL AND t.api_hash <> '') AS has_hash, "
            "(t.session_string IS NOT NULL AND t.session_string <> '') AS has_session, "
            "t.mail, (t.mail_app_pass IS NOT NULL AND t.mail_app_pass <> '') AS has_mailpass, "
            "pr.programs "
            "FROM telegram.telegram t "
            "LEFT JOIN LATERAL ("
            "  SELECT string_agg(p.program_name, ', ' ORDER BY p.program_id) AS programs "
            "  FROM program.programdata p WHERE t.id_telegram = ANY(p.user_bot)"
            ") pr ON true "
            f"{where}ORDER BY {order}",
            func="list_userbots",
        )

    async def get_userbot(self, id_telegram: int) -> asyncpg.Record | None:
        """Полная (для логина нужная) строка юзербота по id_telegram.
        Колонка-FK на Desktop-клиент (config.TG_APP_FIELD) отдаётся под алиасом `my_gram`."""
        field = config.TG_APP_FIELD
        return await self._query(
            f'SELECT id_telegram, name, telegram_name, phone, api_id, api_hash, '
            f'session_string, mail, description, "{field}" AS my_gram '
            f'FROM telegram.telegram WHERE id_telegram = $1',
            id_telegram, mode="row", func="get_userbot",
        )

    # ----- справочник Telegram Desktop клиентов (telegram.<TG_APPS_TABLE>) -----
    async def list_apps(self) -> list[asyncpg.Record]:
        """Все клиенты из справочника (для выбора my_gram в create-флоу)."""
        table = config.TG_APPS_TABLE
        return await self._query(
            f'SELECT app_name, exec_path, workdir, icon, is_system '
            f'FROM telegram."{table}" ORDER BY is_system, app_name',
            func="list_apps",
        )

    async def get_app(self, app_name: str) -> asyncpg.Record | None:
        """Клиент по app_name (= значение my_gram) — exec_path/workdir для запуска."""
        table = config.TG_APPS_TABLE
        return await self._query(
            f'SELECT app_name, exec_path, workdir, icon, is_system '
            f'FROM telegram."{table}" WHERE app_name = $1',
            app_name, mode="row", func="get_app",
        )

    async def programs_using(self, id_telegram: int) -> list[str]:
        """Имена программ, где id_telegram числится в program.programdata.user_bot (int8[]).
        Пустой список — юзербот нигде не привязан. Гейт перед созданием новой сессии:
        предупредить, что id уже используется программой."""
        rows = await self._query(
            "SELECT program_name FROM program.programdata "
            "WHERE $1 = ANY(user_bot) ORDER BY program_id",
            id_telegram, func="programs_using",
        )
        return [r["program_name"] for r in rows]

    async def save_session_string(self, id_telegram: int, session_string: str) -> str:
        """Залить свежий session_string существующему юзерботу."""
        return await self._query(
            "UPDATE telegram.telegram SET session_string = $1 WHERE id_telegram = $2",
            session_string, id_telegram, mode="execute", func="save_session_string",
        )

    async def update_creds(self, id_telegram: int, api_id: int, api_hash: str,
                           phone: str) -> str:
        """Дописать api_id/api_hash/phone (если оператор вписал недостающее в recover-флоу)."""
        return await self._query(
            "UPDATE telegram.telegram SET api_id = $1, api_hash = $2, phone = $3 "
            "WHERE id_telegram = $4",
            api_id, api_hash, phone, id_telegram, mode="execute", func="update_creds",
        )
