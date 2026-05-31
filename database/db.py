"""Доступ к БД Program (asyncpg, через PgBouncer).

Читаем серверы (vocabulary.nodes) и записи программ (program.programdata),
ищем по service_name. Переиспользует ту же БД, что и диспетчер ProgramManager2.0.
"""
import asyncpg

from logs import get_logger
from settings import config

logger = get_logger(__name__)


class Database:
    def __init__(self):
        self._conn: asyncpg.Connection | None = None

    async def connect(self):
        self._conn = await asyncpg.connect(
            user=config.PG_USER, password=config.PG_PASSWORD,
            host=config.PG_HOST, port=config.PG_PORT, database=config.PG_DATABASE,
            statement_cache_size=0,   # обязательно для PgBouncer transaction mode
            timeout=config.SSH_CONNECT_TIMEOUT, command_timeout=15,
        )
        logger.info("БД %s подключена (PgBouncer %s:%s)", config.PG_DATABASE,
                    config.PG_HOST, config.PG_PORT)

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ----- серверы -----
    async def get_online_nodes(self) -> list[asyncpg.Record]:
        """Online-ноды из vocabulary.nodes."""
        return await self._conn.fetch(
            "SELECT id, hostname, server_name, ip_address, description, is_online "
            "FROM vocabulary.nodes WHERE is_online = TRUE "
            "ORDER BY server_name"
        )

    # ----- программы -----
    async def find_programs_by_service(self, service_names: list[str]) -> list[asyncpg.Record]:
        """Записи programdata по списку имён service-файлов."""
        return await self._conn.fetch(
            "SELECT program_id, service_name, folder, status, dispatcher, program_name "
            "FROM program.programdata WHERE service_name = ANY($1) "
            "ORDER BY service_name",
            service_names,
        )

    async def update_program_folder(self, program_id: int, folder: str) -> None:
        """Поправить путь установки в БД (при разрешении конфликта валидации)."""
        await self._conn.execute(
            "UPDATE program.programdata SET folder = $2 WHERE program_id = $1",
            program_id, folder,
        )
        logger.info("programdata.folder обновлён: program_id=%s → %s", program_id, folder)

    # ----- создание записи programdata -----
    async def get_known_authors(self) -> list[asyncpg.Record]:
        """Авторы, реально используемые в programdata (id + имя из telegram.telegram)."""
        return await self._conn.fetch(
            "SELECT p.author, t.name, t.telegram_name, count(*) AS used "
            "FROM program.programdata p "
            "LEFT JOIN telegram.telegram t ON t.id_telegram = p.author "
            "WHERE p.author IS NOT NULL "
            "GROUP BY p.author, t.name, t.telegram_name ORDER BY used DESC"
        )

    async def next_program_id(self) -> int:
        """Следующий program_id (PK без автоинкремента) = MAX+1."""
        val = await self._conn.fetchval(
            "SELECT COALESCE(MAX(program_id), 0) + 1 FROM program.programdata")
        return int(val)

    async def create_program(self, program_id: int, service_name: str, folder: str | None,
                             description: str | None, program_name: str | None,
                             author: int | None) -> None:
        """Минимальная запись programdata. status и dispatcher — явно false (новая программа
        не активна и не ведётся диспетчером, пока не настроена); FK-поля ботов/cookies — NULL."""
        await self._conn.execute(
            "INSERT INTO program.programdata "
            "(program_id, service_name, folder, description, program_name, author, status, dispatcher) "
            "VALUES ($1, $2, $3, $4, $5, $6, false, false)",
            program_id, service_name, folder, description, program_name, author,
        )
        logger.info("programdata: создана запись program_id=%s (%s), status=false dispatcher=false",
                    program_id, service_name)

    async def get_program_nodes(self, program_id: int) -> list[asyncpg.Record]:
        """Ноды, где работает программа (program_data_view: leader/standby).
        Используется для предвыбора целей деплоя."""
        return await self._conn.fetch(
            "SELECT node_id, server_name, ip_address, service_status, rang "
            "FROM \"program\".program_data_view "
            "WHERE program_id = $1 AND is_online = TRUE ORDER BY rang",
            program_id,
        )

    # ----- привязка сервиса к серверу (dispatcher.service_status) -----
    async def bind_service_node(self, service_id: int, node_id: int, status: str = "standby") -> str:
        """Привязать сервис к ноде. Новая строка — со status (по умолч. 'standby');
        существующую НЕ трогаем по статусу (чтобы не понизить leader) — только last_updated.
        Возвращает 'inserted' | 'kept' (что фактически произошло)."""
        row = await self._conn.fetchrow(
            "INSERT INTO dispatcher.service_status (service_id, node_id, status) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (service_id, node_id) DO UPDATE SET last_updated = now() "
            "RETURNING (xmax = 0) AS inserted, status",
            service_id, node_id, status,
        )
        return "inserted" if row["inserted"] else f"kept:{row['status']}"

    async def update_service_state(self, service_id: int, node_id: int,
                                   running: bool, systemd_error: str | None) -> None:
        """Записать фактическое состояние сервиса на ноде (только существующая привязка;
        leader/standby не трогаем)."""
        await self._conn.execute(
            "UPDATE dispatcher.service_status "
            "SET running = $3, systemd_error = $4, last_running_update = now() "
            "WHERE service_id = $1 AND node_id = $2",
            service_id, node_id, running, systemd_error,
        )

    # ----- управление через диспетчер (dispatcher.watchdog_instruction) -----
    async def queue_instruction(self, service_name: str, command: str, node_id: int,
                                source: str = "dm") -> int:
        """Поставить инструкцию watchdog'у (start/stop/restart). Исполняет агент на ноде.
        source='dm' — отличаем от 'gd' (GlobalDispatcher). Возвращает instruction_id."""
        return await self._conn.fetchval(
            "INSERT INTO dispatcher.watchdog_instruction (service_name, command, node_id, source) "
            "VALUES ($1, $2, $3, $4) RETURNING instruction_id",
            service_name, command, node_id, source,
        )

    async def get_instruction(self, instruction_id: int) -> asyncpg.Record | None:
        """Состояние инструкции (для ожидания результата)."""
        return await self._conn.fetchrow(
            "SELECT is_executed, executed_at, result FROM dispatcher.watchdog_instruction "
            "WHERE instruction_id = $1",
            instruction_id,
        )

    async def unbind_service_node(self, service_id: int, node_id: int) -> str:
        """Снять привязку сервиса к ноде (deinstall). Возвращает тег команды (напр. 'DELETE 1')."""
        return await self._conn.execute(
            "DELETE FROM dispatcher.service_status WHERE service_id = $1 AND node_id = $2",
            service_id, node_id,
        )

    async def get_service_bindings(self, service_id: int) -> list[asyncpg.Record]:
        """Все привязки сервиса к нодам (для отчёта)."""
        return await self._conn.fetch(
            "SELECT ss.node_id, ss.status, ss.running, n.server_name, n.ip_address "
            "FROM dispatcher.service_status ss "
            "JOIN vocabulary.nodes n ON n.id = ss.node_id "
            "WHERE ss.service_id = $1 ORDER BY ss.status, n.server_name",
            service_id,
        )
