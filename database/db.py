"""Доступ к БД Program (asyncpg, через PgBouncer).

Читаем серверы (vocabulary.nodes) и записи программ (program.programdata),
ищем по service_name. Переиспользует ту же БД, что и диспетчер ProgramManager2.0.

Соединение — через ПУЛ asyncpg (а не одиночный коннект): инструмент висит
открытым в GUI часами, PgBouncer/сеть рвут idle-соединения между операциями.
Единый `_query` ретраит «оживляемые» обрывы и при нужде пересоздаёт пул;
при окончательном сбое — ПРОБРАСЫВАЕТ исключение (callers полагаются на это,
никто не проверяет False). Неидемпотентные INSERT (watchdog/журнал/создание
записи) идут без ретраев (`retry=False`) — чтобы обрыв на полпути не задвоил
команду/строку; пул при этом всё равно лечим.
"""
import asyncio
import json
from typing import Awaitable, cast

import asyncpg
from asyncpg.exceptions import (CannotConnectNowError, ConnectionDoesNotExistError,
                                InterfaceError)

from logs import get_logger
from settings import config

logger = get_logger(__name__)

# Сообщения PgBouncer/сети, после которых запрос имеет смысл повторить.
_PGBOUNCER_RECOVERABLE = (
    "got result for unknown protocol state",
    "client_login_timeout",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "canceling statement due to",
)

# Таймаут ожидания свободного соединения из пула (правило: не зависать).
_ACQUIRE_TIMEOUT = 30


class Database:
    def __init__(self, min_size: int = 1, max_size: int = 5):
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()

    async def _connect_pool(self, retries: int = 5, delay: float = 2.0):
        for attempt in range(1, retries + 1):
            try:
                self._pool = await cast(Awaitable[asyncpg.Pool], asyncpg.create_pool(
                    user=config.PG_USER, password=config.PG_PASSWORD,
                    host=config.PG_HOST, port=config.PG_PORT, database=config.PG_DATABASE,
                    min_size=self.min_size, max_size=self.max_size,
                    statement_cache_size=0,   # обязательно для PgBouncer transaction mode
                    timeout=config.SSH_CONNECT_TIMEOUT, command_timeout=15,
                ))
                logger.info("БД %s подключена (PgBouncer %s:%s, пул min=%s max=%s)",
                            config.PG_DATABASE, config.PG_HOST, config.PG_PORT,
                            self.min_size, self.max_size)
                return
            except (CannotConnectNowError, ConnectionRefusedError, OSError,
                    TimeoutError, asyncio.TimeoutError) as error:
                logger.warning("⚠️ Попытка %s/%s подключения к БД: %s", attempt, retries, error)
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                else:
                    logger.error("❌ Не удалось подключиться к БД после всех попыток")
                    raise

    async def connect(self, retries: int = 5, delay: float = 2.0):
        """Поднять пул. Идемпотентно: уже поднятый пул не пересоздаём (иначе утечка)."""
        if self._pool is not None:
            return
        await self._connect_pool(retries=retries, delay=delay)

    async def close(self):
        if self._pool is not None:
            try:
                await self._pool.close()
                logger.info("Пул БД закрыт")
            except (Exception,) as error:
                logger.warning("Ошибка закрытия пула БД: %s", error)
            self._pool = None

    async def _recreate_pool(self):
        """Health-check + пересоздание пула под локом (другой таск мог уже починить)."""
        async with self._pool_lock:
            pool = self._pool
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
            self._pool = None
            logger.warning("Пересоздаю пул БД")
            await self._connect_pool()

    @staticmethod
    def _is_pre_send(error: Exception) -> bool:
        """True, если соединение заведомо мертво ДО отправки запроса (запрос не выполнялся)
        → повтор безопасен даже для неидемпотентных INSERT. Это и есть «connection is closed»
        от idle-обрыва PgBouncer (баг, ради которого появился пул)."""
        if isinstance(error, ConnectionDoesNotExistError):
            return True
        msg = str(error).lower()
        return isinstance(error, InterfaceError) and (
            "connection is closed" in msg or "pool is closing" in msg
            or "connection was closed" in msg or "is being released" in msg)

    @staticmethod
    def _is_ambiguous(error: Exception) -> bool:
        """True, если обрыв мог случиться «в полёте» — запрос, возможно, выполнился.
        Повторяем только идемпотентные (retry=True); неидемпотентные — пробрасываем."""
        if isinstance(error, (InterfaceError, CannotConnectNowError,
                              ConnectionDoesNotExistError, TimeoutError, asyncio.TimeoutError)):
            return True
        return any(m in str(error) for m in _PGBOUNCER_RECOVERABLE)

    async def _query(self, sql: str, *args, mode: str = "all", func: str = "",
                     retry: bool = True, retries: int = 3, delay: float = 2.0):
        """Запрос через пул с ретраями и пересозданием пула. mode:
        'all' → list[Record], 'row' → Record|None, 'val' → значение|None,
        'execute' → строка-статус (напр. 'DELETE 1'). Контракт ошибок —
        ИСКЛЮЧЕНИЕ (не False): неоживляемая/окончательная — пробрасывается.
        retry=False (неидемпотентный INSERT): pre-send-обрыв всё равно повторяем
        (запрос не выполнялся), а ambiguous — пробрасываем, чтобы не задвоить."""
        if self._pool is None:
            raise RuntimeError(f"Пул БД не создан — {func or 'запрос'} невозможен")
        for attempt in range(1, retries + 1):
            try:
                async with self._pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                    if mode == "row":
                        return await conn.fetchrow(sql, *args)
                    if mode == "val":
                        return await conn.fetchval(sql, *args)
                    if mode == "all":
                        return await conn.fetch(sql, *args)
                    if mode == "execute":
                        return await conn.execute(sql, *args)
                    raise ValueError(f"Некорректный mode: {mode}")
            except (Exception,) as error:
                pre_send = self._is_pre_send(error)
                if not (pre_send or self._is_ambiguous(error)):
                    # Баг в SQL/параметрах либо реальная ошибка БД — пробрасываем со стеком.
                    logger.error("Ошибка БД в %s: %s", func or "запрос", error, exc_info=True)
                    raise
                can_retry = pre_send or retry
                if can_retry and attempt < retries:
                    logger.warning("Соединение БД разорвано в %s (%s/%s, %s): %s",
                                   func or "запрос", attempt, retries,
                                   "pre-send" if pre_send else "ambiguous", error)
                    await asyncio.sleep(delay * attempt)
                    continue
                # не повторяем (ambiguous + неидемпотентный) либо попытки исчерпаны —
                # лечим пул для следующих операций и пробрасываем
                if not can_retry:
                    logger.warning("Обрыв БД в %s без ретрая (ambiguous, неидемпотентный): %s",
                                   func or "запрос", error)
                try:
                    await self._recreate_pool()
                except (Exception,) as pool_error:
                    logger.error("Не удалось пересоздать пул БД: %s", pool_error)
                raise
        raise RuntimeError(f"_query({func}): исчерпаны попытки")  # недостижимо

    # ----- серверы -----
    async def get_online_nodes(self) -> list[asyncpg.Record]:
        """Online-ноды из vocabulary.nodes."""
        return await self._query(
            "SELECT id, hostname, server_name, ip_address, description, is_online, claster "
            "FROM vocabulary.nodes WHERE is_online = TRUE "
            "ORDER BY server_name",
            func="get_online_nodes",
        )

    # ----- программы -----
    async def find_programs_by_service(self, service_names: list[str]) -> list[asyncpg.Record]:
        """Записи programdata по списку имён service-файлов."""
        return await self._query(
            "SELECT program_id, service_name, folder, status, dispatcher, program_name "
            "FROM program.programdata WHERE service_name = ANY($1) "
            "ORDER BY service_name",
            service_names, func="find_programs_by_service",
        )

    async def list_programs(self) -> list[asyncpg.Record]:
        """Все программы из programdata (для деинсталляции «из БД», в т.ч. старые)."""
        return await self._query(
            "SELECT program_id, service_name, folder, status, dispatcher, program_name "
            "FROM program.programdata ORDER BY service_name",
            func="list_programs",
        )

    async def delete_program(self, program_id: int) -> str:
        """Удалить запись программы (каскадно — service_status/cron/option_setting/… по FK)."""
        return await self._query(
            "DELETE FROM program.programdata WHERE program_id = $1", program_id,
            mode="execute", func="delete_program",
        )

    async def update_program_folder(self, program_id: int, folder: str) -> None:
        """Поправить путь установки в БД (при разрешении конфликта валидации)."""
        await self._query(
            "UPDATE program.programdata SET folder = $2 WHERE program_id = $1",
            program_id, folder, mode="execute", func="update_program_folder",
        )
        logger.info("programdata.folder обновлён: program_id=%s → %s", program_id, folder)

    # ----- создание записи programdata -----
    async def get_known_authors(self) -> list[asyncpg.Record]:
        """Авторы, реально используемые в programdata (id + имя из telegram.telegram)."""
        return await self._query(
            "SELECT p.author, t.name, t.telegram_name, count(*) AS used "
            "FROM program.programdata p "
            "LEFT JOIN telegram.telegram t ON t.id_telegram = p.author "
            "WHERE p.author IS NOT NULL "
            "GROUP BY p.author, t.name, t.telegram_name ORDER BY used DESC",
            func="get_known_authors",
        )

    async def create_program(self, service_name: str, folder: str | None,
                             description: str | None, program_name: str | None,
                             author: int | None, dispatcher: bool = False) -> int:
        """Запись programdata; program_id генерирует БД (IDENTITY) — НЕ передаём его в INSERT.
        status — всегда false (новая программа не активна, пока не настроена); dispatcher выбирает
        оператор; FK ботов/cookies — NULL. Возвращает сгенерированный program_id."""
        program_id = await self._query(
            "INSERT INTO program.programdata "
            "(service_name, folder, description, program_name, author, status, dispatcher) "
            "VALUES ($1, $2, $3, $4, $5, false, $6) RETURNING program_id",
            service_name, folder, description, program_name, author, dispatcher,
            mode="val", func="create_program", retry=False,  # авто-PK → повтор задвоит строку
        )
        logger.info("programdata: создана запись program_id=%s (%s), status=false dispatcher=%s",
                    program_id, service_name, dispatcher)
        return int(program_id)

    async def get_program_nodes(self, program_id: int) -> list[asyncpg.Record]:
        """Ноды, где работает программа (program_data_view: leader/standby).
        Используется для предвыбора целей деплоя."""
        return await self._query(
            "SELECT node_id, server_name, ip_address, service_status, rang "
            "FROM \"program\".program_data_view "
            "WHERE program_id = $1 AND is_online = TRUE ORDER BY rang",
            program_id, func="get_program_nodes",
        )

    # ----- привязка сервиса к серверу (dispatcher.service_status) -----
    async def bind_service_node(self, service_id: int, node_id: int, status: str = "standby") -> str:
        """Привязать сервис к ноде. Новая строка — со status (по умолч. 'standby');
        существующую НЕ трогаем по статусу (чтобы не понизить leader) — только last_updated.
        Возвращает 'inserted' | 'kept' (что фактически произошло)."""
        row = await self._query(
            "INSERT INTO dispatcher.service_status (service_id, node_id, status) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (service_id, node_id) DO UPDATE SET last_updated = now() "
            "RETURNING (xmax = 0) AS inserted, status",
            service_id, node_id, status, mode="row", func="bind_service_node",
        )
        return "inserted" if row["inserted"] else f"kept:{row['status']}"

    async def update_service_state(self, service_id: int, node_id: int,
                                   running: bool, systemd_error: str | None) -> None:
        """Записать фактическое состояние сервиса на ноде (только существующая привязка;
        leader/standby не трогаем)."""
        await self._query(
            "UPDATE dispatcher.service_status "
            "SET running = $3, systemd_error = $4, last_running_update = now() "
            "WHERE service_id = $1 AND node_id = $2",
            service_id, node_id, running, systemd_error,
            mode="execute", func="update_service_state",
        )

    # ----- управление через диспетчер: §13 (control_request → GD), см. ниже -----
    # (raw insert_dm_event/queue_instruction/get_instruction убраны 2026-06-26 —
    #  миграция управления на control_request; GD сам размещает и верифицирует.)

    # ----- §13: управление через GlobalDispatcher (control_request), не raw -----
    async def enable_dispatcher(self, program_id: int) -> int | None:
        """
        Включить диспетчера для программы (programdata.dispatcher=true). Нужно ДО
        подачи control_request: GD управляет ТОЛЬКО dispatcher=true (иначе пометит
        намерение терминалом 'NonDispatcher'). Возвращает program_id если флаг
        реально подняли (был false), иначе None (уже был true) — для отчёта."""
        return await self._query(
            "UPDATE program.programdata SET dispatcher = true "
            "WHERE program_id = $1 AND dispatcher IS DISTINCT FROM true "
            "RETURNING program_id",
            program_id, mode="val", func="enable_dispatcher",
        )

    async def submit_control_request(self, program_id: int, service_name: str,
                                     command: str, source: str = "dm") -> int:
        """
        Подать намерение (start/stop/restart) в dispatcher.control_request (source='dm').
        GD CronRequestHandler подберёт placement и исполнит через lifecycle (desired-state
        status ставит сам GD). Ни SSH, ни raw watchdog_instruction. Возвращает request_id.
        retry=False — не задвоить намерение при ambiguous-обрыве."""
        return await self._query(
            "INSERT INTO dispatcher.control_request "
            "(program_id, service_name, command, status, source) "
            "VALUES ($1, $2, $3, 'pending', $4) RETURNING request_id",
            program_id, service_name, command, source,
            mode="val", func="submit_control_request", retry=False,
        )

    async def poll_request(self, request_id: int) -> asyncpg.Record | None:
        """
        Снимок исхода намерения: req_status pending→approved→completed/failed/
        cancelled/NonDispatcher (последний — программа вне власти GD). actual_node_id —
        куда GD разместил. instr_status — статус связанной watchdog_instruction."""
        return await self._query(
            "SELECT cr.status AS req_status, cr.decided_reason, cr.completion_result, "
            "       cr.actual_node_id, wi.status AS instr_status "
            "FROM dispatcher.control_request cr "
            "LEFT JOIN dispatcher.watchdog_instruction wi "
            "       ON wi.instruction_id = cr.instruction_id "
            "WHERE cr.request_id = $1",
            request_id, mode="row", func="poll_request",
        )

    async def unbind_service_node(self, service_id: int, node_id: int) -> str:
        """Снять привязку сервиса к ноде (deinstall). Возвращает тег команды (напр. 'DELETE 1')."""
        return await self._query(
            "DELETE FROM dispatcher.service_status WHERE service_id = $1 AND node_id = $2",
            service_id, node_id, mode="execute", func="unbind_service_node",
        )

    # ----- журнал действий (dispatcher.deploy_journal) -----
    async def journal_write(self, program_id: int, node_id: int | None, action: str,
                            folder_deployed: bool, service_installed: bool, db_updated: bool,
                            result: str | None, commit: str | None, operator: str | None,
                            details: dict | None = None) -> None:
        await self._query(
            "INSERT INTO dispatcher.deploy_journal "
            "(program_id, node_id, action, folder_deployed, service_installed, db_updated, "
            " result, commit, operator, details) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            program_id, node_id, action, folder_deployed, service_installed, db_updated,
            result, commit, operator, json.dumps(details) if details is not None else None,
            mode="execute", func="journal_write",  # двойная запись журнала косметична → retry ок
        )

    async def journal_programs(self) -> list[asyncpg.Record]:
        """Программы, засветившиеся в журнале деплоя (что ставили этим инструментом) и ещё
        существующие в programdata. Поля как у list_programs + last_ts. Свежие — выше."""
        return await self._query(
            "SELECT p.program_id, p.service_name, p.folder, p.status, p.dispatcher, "
            "p.program_name, max(j.ts) AS last_ts "
            "FROM dispatcher.deploy_journal j "
            "JOIN program.programdata p ON p.program_id = j.program_id "
            "GROUP BY p.program_id, p.service_name, p.folder, p.status, p.dispatcher, p.program_name "
            "ORDER BY last_ts DESC",
            func="journal_programs",
        )

    async def journal_recent(self, program_id: int) -> list[asyncpg.Record]:
        """Последнее состояние журнала по каждой ноде для программы (для поиска «по журналу»)."""
        return await self._query(
            "SELECT DISTINCT ON (node_id) node_id, action, folder_deployed, service_installed, "
            "db_updated, result, ts FROM dispatcher.deploy_journal "
            "WHERE program_id = $1 AND node_id IS NOT NULL ORDER BY node_id, ts DESC",
            program_id, func="journal_recent",
        )

    async def get_service_bindings(self, service_id: int) -> list[asyncpg.Record]:
        """Все привязки сервиса к нодам (для отчёта)."""
        return await self._query(
            "SELECT ss.node_id, ss.status, ss.running, n.server_name, n.ip_address, n.rang "
            "FROM dispatcher.service_status ss "
            "JOIN vocabulary.nodes n ON n.id = ss.node_id "
            "WHERE ss.service_id = $1 ORDER BY ss.status, n.server_name",
            service_id, func="get_service_bindings",
        )

