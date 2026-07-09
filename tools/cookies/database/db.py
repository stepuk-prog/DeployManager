"""Асинхронный доступ к двум БД (asyncpg, через PgBouncer transaction mode).

Два пула на одном PG-инстансе (отличается только имя БД):
  'program'  — почта/имена аккаунтов, settings.pocket_settings, cookies.* (pocket/tv),
               telegram.telegram, program.programdata.
  'binodex'  — settings.binodex_settings (селекторы), settings.option_setting,
               cookies.binodex_cookies (storage_state).

Кросс-БД JOIN невозможен → данные собираем в два запроса и сшиваем в Python (см. gui/flows.py).

Контракт (правило #10): execute_query при ОШИБКЕ → False (не None). Успех — результат:
list ('all'), Record|None ('row'), значение|None ('val'), True ('execute'). None из
'row'/'val' = «строки нет», False = «сбой» — различимы.

jsonb-codec обязателен (иначе jsonb приходит/уходит строкой). Колонки cookies.* — jsonb:
в save_* передаём Python list/dict (НЕ json-строку), кодек сам сериализует.
statement_cache_size=0 обязателен для PgBouncer transaction mode.
"""
import asyncio
import json
from typing import Awaitable, cast

import asyncpg
from asyncpg.exceptions import (CannotConnectNowError, ConnectionDoesNotExistError,
                                InterfaceError)

from tools.cookies.logs import init_logger
from tools.cookies.settings import config

logger = init_logger(__name__)

# Сообщения PgBouncer/сети, после которых запрос имеет смысл повторить.
_PGBOUNCER_RECOVERABLE = (
    "got result for unknown protocol state",
    "client_login_timeout",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "canceling statement due to",
)

# Таймаут ожидания свободного соединения из пула (правило #6: не зависать).
_ACQUIRE_TIMEOUT = 30


async def _init_json_codec(conn: asyncpg.Connection) -> None:
    """json/jsonb codec — иначе asyncpg отдаёт/принимает их как str."""
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


class Database:
    """Два пула asyncpg с ретраями и пересозданием пула при обрыве."""

    def __init__(self, min_size: int = 1, max_size: int = 5):
        self.min_size = min_size
        self.max_size = max_size
        self._pools: dict[str, asyncpg.Pool | None] = {"program": None, "binodex": None}
        self._pool_locks: dict[str, asyncio.Lock] = {
            "program": asyncio.Lock(), "binodex": asyncio.Lock(),
        }

    async def _connect_pool(self, name: str, retries: int = 5, delay: float = 2.0):
        db_name = config.DB_NAMES[name]
        for attempt in range(1, retries + 1):
            try:
                pool_factory = cast(Awaitable[asyncpg.Pool], asyncpg.create_pool(
                    user=config.PG_USER, password=config.PG_PASSWORD,
                    host=config.PG_HOST, port=config.PG_PORT, database=db_name,
                    min_size=self.min_size, max_size=self.max_size,
                    statement_cache_size=0,   # обязательно для PgBouncer transaction mode
                    timeout=10,               # connect timeout
                    command_timeout=30,       # не зависать на мёртвом соединении
                    init=_init_json_codec,
                ))
                self._pools[name] = await pool_factory
                logger.info("✅ Пул '%s' (→ %s) создан (min=%s max=%s)",
                            name, db_name, self.min_size, self.max_size)
                return
            except (CannotConnectNowError, ConnectionRefusedError, OSError,
                    TimeoutError, asyncio.TimeoutError) as error:
                logger.warning("⚠️ Попытка %s/%s пула '%s': %s", attempt, retries, name, error)
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                else:
                    logger.error("❌ Не удалось создать пул '%s' после всех попыток", name)
                    raise

    async def connect(self, retries: int = 5, delay: float = 2.0):
        """Поднять оба пула. Идемпотентно. При частичном сбое — закрыть всё и пробросить."""
        try:
            for name in ("program", "binodex"):
                if self._pools[name] is None:
                    await self._connect_pool(name, retries=retries, delay=delay)
        except (Exception,):
            await self.close()
            raise

    async def close(self):
        for name, pool in list(self._pools.items()):
            if pool is not None:
                try:
                    await pool.close()
                    logger.info("Пул '%s' закрыт", name)
                except (Exception,) as error:
                    logger.warning("Ошибка закрытия пула '%s': %s", name, error)
                self._pools[name] = None

    async def _recreate_pool(self, name: str):
        async with self._pool_locks[name]:
            pool = self._pools[name]
            if pool is not None:
                try:
                    async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                        await conn.fetchval("SELECT 1")
                    return
                except (Exception,):
                    pass
                try:
                    await pool.close()
                except (Exception,):
                    pass
            self._pools[name] = None
            logger.warning("Пересоздаю пул '%s'", name)
            await self._connect_pool(name)

    async def execute_query(self, sql: str, *args, retries: int = 3, delay: float = 2.0,
                            fetch_mode: str = "all", func: str = "", db: str = "program"):
        """Запрос через пул `db` с ретраями. Контракт ошибок — False (правило #10)."""
        pool = self._pools.get(db)
        if pool is None:
            logger.error("Пул '%s' не создан — %s невозможен", db, func)
            return False
        for attempt in range(1, retries + 1):
            try:
                async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                    if fetch_mode == "row":
                        return await conn.fetchrow(sql, *args)
                    if fetch_mode == "val":
                        return await conn.fetchval(sql, *args)
                    if fetch_mode == "all":
                        return await conn.fetch(sql, *args)
                    if fetch_mode == "execute":
                        await conn.execute(sql, *args)
                        return True
                    logger.error("Некорректный fetch_mode: %s", fetch_mode)
                    return False
            except (InterfaceError, CannotConnectNowError, ConnectionDoesNotExistError,
                    TimeoutError, asyncio.TimeoutError) as error:
                logger.warning("Соединение пула '%s' разорвано в %s (%s/%s): %s",
                               db, func, attempt, retries, error)
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                    continue
                logger.error("%s: пересоздаю пул '%s'", func, db)
                try:
                    await self._recreate_pool(db)
                except (Exception,) as pool_error:
                    logger.error("Не удалось пересоздать пул '%s': %s", db, pool_error)
                return False
            except (Exception,) as error:
                msg = str(error)
                if any(m in msg for m in _PGBOUNCER_RECOVERABLE):
                    logger.warning("PgBouncer (%s): %s — retry %s/%s (%s)",
                                   db, msg, attempt, retries, func)
                    if attempt < retries:
                        await asyncio.sleep(delay * attempt)
                        continue
                    try:
                        await self._recreate_pool(db)
                    except (Exception,) as pool_error:
                        logger.error("Не удалось пересоздать пул '%s': %s", db, pool_error)
                    return False
                # Непредвиденное (вероятно баг в SQL/параметрах) — контракт обязывает вернуть
                # False, но стек НЕ теряем (иначе реальные баги невидимы).
                logger.error("Непредвиденная SQL-ошибка в %s (пул '%s'): %s",
                             func, db, msg, exc_info=True)
                return False
        return False

    # ============================ Program (db='program') ============================

    async def pocket_settings(self):
        """settings.pocket_settings — селекторы OTC (резолв через settings/pocket.py)."""
        return await self.execute_query(
            "SELECT * FROM settings.pocket_settings",
            fetch_mode="all", func="pocket_settings", db="program")

    async def find_otc_option_cookies(self):
        """Аккаунты вкладки OTC Option (option=true). cookies_pocket NOT NULL — это user_id
        для сохранения; без него аккаунт не сохранить."""
        return await self.execute_query(
            "SELECT * FROM cookies.otc_option_cookies "
            "WHERE option = true AND cookies_pocket IS NOT NULL",
            fetch_mode="all", func="find_otc_option_cookies", db="program")

    async def find_otc_other_cookies(self):
        """Вторая группа кнопок «Прочее» на вкладке OTC Option. Фильтр cookies_pocket NOT NULL
        отсекает подмешанные во вьюху TradingView-аккаунты (у них cookies_pocket пуст)."""
        return await self.execute_query(
            "SELECT * FROM cookies.otc_other_cookies_view WHERE cookies_pocket IS NOT NULL",
            fetch_mode="all", func="find_otc_other_cookies", db="program")

    async def find_otc_screen_cookies(self, meta: bool):
        """Аккаунты вкладки OTC Screen (meta=false). cookies_pocket NOT NULL — см. выше."""
        return await self.execute_query(
            "SELECT * FROM cookies.otc_screen_cookies_view "
            "WHERE meta = $1 AND cookies_pocket IS NOT NULL", meta,
            fetch_mode="all", func="find_otc_screen_cookies", db="program")

    async def find_tv_cookies(self):
        """Аккаунты вкладки TradingView."""
        sql = """
            SELECT user_id,
                   name AS google_account_name,
                   mail,
                   mail_pass AS tv_pass,
                   cookies,
                   updated_at
            FROM cookies.tv_screen_cookies_view
        """
        return await self.execute_query(sql, fetch_mode="all", func="find_tv_cookies",
                                        db="program")

    async def tv_new_accounts(self):
        """Аккаунты для режима «Добавить новый» (TradingView): заполнен tv_pass, но ещё НЕТ
        строки в cookies.tv_cookies. Обе таблицы в БД Program → отсечение существующих прямо в
        SQL через NOT EXISTS (кросс-БД склейка не нужна, в отличие от binodex).
        list[Record(id_telegram, name, mail, tv_pass)] | [] | False."""
        sql = """
            SELECT t.id_telegram, t.name, t.mail, t.tv_pass
            FROM telegram.telegram t
            WHERE t.tv_pass IS NOT NULL AND t.tv_pass <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM cookies.tv_cookies tc WHERE tc.user_id = t.id_telegram
              )
            ORDER BY t.name
        """
        return await self.execute_query(sql, fetch_mode="all",
                                        func="tv_new_accounts", db="program")

    async def save_pocket_cook(self, user_id: int, cookies: list):
        """Upsert нормализованных OTC cookies (cookies.pocket_cookies.cookies = jsonb →
        передаём list, кодек сериализует). True | False."""
        sql = ("INSERT INTO cookies.pocket_cookies (user_id, cookies) VALUES ($1, $2) "
               "ON CONFLICT (user_id) DO UPDATE "
               "SET cookies = EXCLUDED.cookies, updated_at = now()")
        return await self.execute_query(sql, user_id, cookies, fetch_mode="execute",
                                        func="save_pocket_cook", db="program")

    async def save_tv_cook(self, user_id: int, cookies: list):
        """Upsert нормализованных TradingView cookies (jsonb → list)."""
        sql = ("INSERT INTO cookies.tv_cookies (user_id, cookies) VALUES ($1, $2) "
               "ON CONFLICT (user_id) DO UPDATE "
               "SET cookies = EXCLUDED.cookies, updated_at = now()")
        return await self.execute_query(sql, user_id, cookies, fetch_mode="execute",
                                        func="save_tv_cook", db="program")

    async def get_mail_creds(self, id_telegram: int):
        """mail + Gmail app-password владельца аккаунта (для IMAP-логина binodex).
        Record(mail, mail_app_pass) | None | False."""
        return await self.execute_query(
            "SELECT mail, mail_app_pass FROM telegram.telegram WHERE id_telegram = $1",
            id_telegram, fetch_mode="row", func="get_mail_creds", db="program")

    async def get_program_names(self, program_ids: list):
        """program_id → program_name (для подписей кнопок binodex «Обновить старый»).
        list[Record(program_id, program_name)] | [] | False."""
        if not program_ids:
            return []
        return await self.execute_query(
            "SELECT program_id, program_name FROM program.programdata "
            "WHERE program_id = ANY($1)", program_ids,
            fetch_mode="all", func="get_program_names", db="program")

    async def telegram_new_accounts(self):
        """Аккаунты для режима «Добавить новый»: заполнены mail, mail_app_pass, api_id,
        api_hash. Отсечение уже существующих (есть строка в binodex_cookies) — в Python
        (кросс-БД), см. gui/flows.py. list[Record(id_telegram, name, mail, mail_app_pass)]."""
        sql = """
            SELECT id_telegram, name, mail, mail_app_pass
            FROM telegram.telegram
            WHERE mail IS NOT NULL AND mail <> ''
              AND mail_app_pass IS NOT NULL AND mail_app_pass <> ''
              AND api_id IS NOT NULL
              AND api_hash IS NOT NULL AND api_hash <> ''
            ORDER BY name
        """
        return await self.execute_query(sql, fetch_mode="all",
                                        func="telegram_new_accounts", db="program")

    # ============================ binodex (db='binodex') ============================

    async def binodex_selectors(self):
        """Все CSS-селекторы binodex (login_*/setup_*). list[Record(par_name, par_value)]."""
        return await self.execute_query(
            "SELECT par_name, par_value FROM settings.binodex_settings",
            fetch_mode="all", func="binodex_selectors", db="binodex")

    async def binodex_option_accounts(self):
        """Аккаунты режима «Обновить старый»: строки option_setting с заполненным
        cookies_pocket, дедуп по cookies_pocket. list[Record(cookies_pocket, program_id)]."""
        sql = """
            SELECT DISTINCT ON (cookies_pocket) cookies_pocket, program_id, prog_name
            FROM settings.option_setting
            WHERE cookies_pocket IS NOT NULL
            ORDER BY cookies_pocket, program_id
        """
        return await self.execute_query(sql, fetch_mode="all",
                                        func="binodex_option_accounts", db="binodex")

    async def binodex_existing_user_ids(self):
        """user_id, у кого уже есть storage_state в binodex_cookies (для «Добавить новый»).
        list[Record(user_id)] | [] | False."""
        return await self.execute_query(
            "SELECT user_id FROM cookies.binodex_cookies",
            fetch_mode="all", func="binodex_existing_user_ids", db="binodex")

    async def save_binodex_state(self, user_id: int, storage_state: dict):
        """Upsert Privy storage_state в cookies.binodex_cookies (jsonb → dict). True | False."""
        sql = ("INSERT INTO cookies.binodex_cookies (user_id, cookies, updated_at) "
               "VALUES ($1, $2, now()) "
               "ON CONFLICT (user_id) DO UPDATE "
               "SET cookies = EXCLUDED.cookies, updated_at = EXCLUDED.updated_at")
        return await self.execute_query(sql, user_id, storage_state, fetch_mode="execute",
                                        func="save_binodex_state", db="binodex")

    async def get_active_proxies(self, scope: str = "binodex"):
        """Активные :50100 (HTTP) прокси для BROWSER из settings.proxy_data (как в BinoOptions).
        Cookies-инструмент всегда binodex-scope → фильтр по бан-полям *_binodex; ротация —
        свежайший last_used_at_binodex в хвост. list[dict(ip,port,login,password)] | [] | False."""
        sql = (
            "SELECT ip, port, login, password FROM settings.proxy_data "
            "WHERE is_active = true AND port = 50100 "
            "AND long_ban_binodex = false "
            "AND (is_banned_binodex = false OR banned_until_binodex < now()) "
            "ORDER BY priority DESC, last_used_at_binodex ASC NULLS FIRST"
        )
        rows = await self.execute_query(sql, fetch_mode="all", func="get_active_proxies",
                                        db="binodex")
        if not rows:
            return rows  # [] | False — прокинуть наверх без маскировки
        return [{"ip": r["ip"], "port": r["port"], "login": r["login"],
                 "password": r["password"]} for r in rows]
