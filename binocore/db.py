"""Асинхронный доступ к PostgreSQL через asyncpg: пулы, ретраи, самовосстановление.

Ядро слоя БД, общее для программ семьи. Программа наследует `BaseDatabase` и добавляет
свои SQL-методы; всё, что ниже, у всех одинаково.

Модель — ИМЕНОВАННЫЕ ПУЛЫ на одном инстансе: `db_names` задаёт соответствие
«логическое имя → имя базы», `execute_query(..., db=...)` выбирает пул. Один объект на
все базы: общая политика ретраев и один флаг антиспама на программу. Альтернатива (по
объекту `Database` на каждую базу) разводила состояние восстановления по копиям и
заставляла вызывающего помнить, к какому объекту идти за каким запросом.

Тексты логов — в `MESSAGES`, переопределяются через `configure()`: у английских форков
часть строк английская. Логгер тоже передаётся программой: у семьи свой `init_logger`
(доп. уровни, файлы по уровням, отправка в Telegram).
"""
import asyncio
import logging
import random
from typing import Awaitable, cast

import asyncpg
from asyncpg.exceptions import (CannotConnectNowError, ConnectionDoesNotExistError,
                                InterfaceError, ReadOnlySQLTransactionError)

_logger = logging.getLogger(__name__)


# Ошибки, которые лечатся повтором, а не падением: PgBouncer перезапускается/флапает, Patroni
# переключает лидера, сеть моргает. Один список на пул и на одиночный коннект — политика должна
# быть одна, иначе получается то, что нашли 15-09-2026: рантайм блип переживает, а старт от того
# же блипа умирает.
# Публичное имя: этот же список нужен стартовому чтению конфига (settings/_bootstrap) —
# по нему отличают «БД недоступна» (транзиент, код выхода 1, диспетчер поднимет заново) от
# «схема/данные не те» (нужен человек, код 12). Второй такой список в программе означал бы
# второй источник истины: расширили здесь — там бы молча осталось старое.
CONNECT_RETRYABLE = (CannotConnectNowError, ConnectionRefusedError, OSError,
                     TimeoutError, asyncio.TimeoutError)


async def connect_with_retry(retries: int = 5, delay: float = 2.0, init=None,
                             on_retry=None, **dsn):
    """Одиночный `asyncpg.connect` с той же политикой ретраев, что и у пулов.

    Зачем отдельно от пула: стартовое чтение конфига (settings/_bootstrap) идёт ДО asyncio.run и
    до логгера, пулов ещё нет — а PgBouncer в этот момент точно так же может флапнуть. Раньше там
    стояла одна попытка: блип на старте убивал процесс на ИМПОРТЕ (в logs/ ни строки, видно
    только в journald), тогда как тот же блип в рантайме переживался молча. Поймано живым запуском
    15-09-2026 — четыре падения за сессию. Реестр: bootstrap-connect-retry.

    `init` — корутина донастройки соединения (у семьи это json/jsonb-кодек), зовётся после
    успешного connect. `on_retry(attempt, retries, error)` — уведомление о повторе: на стартовом
    пути логгер программы ещё не сконфигурирован, поэтому вызывающий обычно передаёт print в
    stderr (его забирает journald). По умолчанию — _logger.warning, как у пулов.

    Задержка растёт линейно (delay * attempt), как в _connect_pool: суммарно ~30 с на пять
    попыток. Больше не нужно — PgBouncer поднимается за секунды, а юнит под systemd всё равно
    будет перезапущен, если не поднялись мы.
    """
    # max(1, ...): при retries <= 0 цикл не выполнился бы ни разу, и функция ВЕРНУЛА БЫ None —
    # вызывающий получил бы «коннект» вместо ошибки и упал бы позже и не там (на .fetch у None).
    # Один проход — минимум осмысленного: «не ретраить» это одна попытка, а не ноль.
    attempts = max(1, retries)
    last_error: Exception = RuntimeError('connect_with_retry: не было ни одной попытки')
    for attempt in range(1, attempts + 1):
        try:
            conn = await asyncpg.connect(**dsn)
            if init is not None:
                await init(conn)
            return conn
        except CONNECT_RETRYABLE as error:
            last_error = error
            if attempt >= attempts:
                break
            if on_retry is not None:
                on_retry(attempt, attempts, error)
            else:
                _logger.warning(_msg('pool_attempt', attempt=attempt, retries=attempts,
                                     name='bootstrap', error=error))
            await asyncio.sleep(delay * attempt)
    # Выход из цикла означает исчерпанные попытки — отдаём последнюю ошибку наружу. raise ЗДЕСЬ,
    # а не внутри except: иначе у функции остаётся путь «дошли до конца и вернули None», и это
    # видит не только проверяющий типов, но и рантайм при retries <= 0.
    raise last_error

# Подстроки ошибок PgBouncer/Patroni, которые лечатся ретраем, а не падением.
_PGBOUNCER_RECOVERABLE = (
    "got result for unknown protocol state",
    "client_login_timeout",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    # ТОЛЬКО конфликт с восстановлением (отмена на standby при failover), НЕ голое
    # "canceling statement due to": оно матчило бы и statement_timeout/lock_timeout/
    # user-cancel, и настоящий медленный (багнутый) запрос ретраился бы как «транзиент»,
    # утраивая нагрузку на и без того тяжёлом месте. Узкий вариант был найден в
    # BinoStoch и EnglishStock и до остальных программ не доехал — ядро берёт его.
    "canceling statement due to conflict with recovery",
    # После Patroni failover старый лидер демотится в read-only standby; PgBouncer ещё
    # раздаёт серверные соединения к нему → UPDATE падает «read-only transaction». Лечится
    # ретраем (новая транзакция → writable-лидер) + пересозданием пула — поэтому recoverable.
    "read-only transaction",
    "только чтение",
)

# Таймаут ожидания свободного соединения из пула (правило: не зависать).
_ACQUIRE_TIMEOUT = 30

# Тексты логов. Дефолт — русский (11 программ из 12); английские форки передают свои
# через configure(messages=...). Ключи стабильны, менять их — ломать переопределения.
MESSAGES = {
    'pool_created': "✅ Пул '{name}' (→ {db_name}) создан (min={min_size}, max={max_size})",
    'pool_attempt': "⚠️ Попытка {attempt}/{retries} пула '{name}': {error}",
    'pool_create_failed': "❌ Не удалось создать пул '{name}' после всех попыток",
    'pool_closed': "Пул '{name}' закрыт",
    'pool_close_error': "Ошибка закрытия пула '{name}': {error}",
    'pool_recreating': "Пересоздаю пул '{name}'",
    'pool_missing': "Пул '{db}' не создан — {func} невозможен",
    'bad_fetch_mode': "Некорректный fetch_mode: {fetch_mode}",
    'unexpected_sql': "Непредвиденная SQL-ошибка в {func} (пул '{db}'): {msg}",
    'connection_dropped': "Соединение пула '{db}' разорвано в {func} ({attempt}/{retries}): {error}",
    'recreate_after_retries': "{func}: пересоздаю пул '{db}'",
    'recreate_failed': "Не удалось пересоздать пул '{db}': {error}",
    'restore_failed': "Не удалось восстановить соединение пула '{db}' после всех попыток",
}


def configure(logger=None, messages: dict = None) -> None:
    """Подключить логгер программы и, при необходимости, свои тексты сообщений.

    Зовётся один раз при импорте `database/database.py` программы — там логгер уже создан,
    а кругов импорта нет (в отличие от `settings/*`, откуда звать нельзя). Без вызова слой
    пишет в стандартный logging и до файлов/Telegram программы записи не доходят:
    init_logger семьи вешает хендлеры на именованный логгер с propagate=False.

    `messages` — частичный словарь: переопределяются только переданные ключи."""
    global _logger
    if logger is not None:
        _logger = logger
    if messages:
        unknown = set(messages) - set(MESSAGES)
        assert not unknown, f'неизвестные ключи сообщений: {sorted(unknown)}'
        MESSAGES.update(messages)


def _msg(key: str, **kw) -> str:
    return MESSAGES[key].format(**kw)


class BaseDatabase:
    """Пулы соединений + единая политика ретраев. SQL-методы — в наследнике."""

    def __init__(self, db_names: dict, *, user, password, host, port, init=None,
                 min_size: int = 2, max_size: int = 10, command_timeout: int = 30):
        self.db_names = dict(db_names)
        self._dsn = dict(user=user, password=password, host=host, port=port)
        self._init = init
        self.min_size = min_size
        self.max_size = max_size
        self.command_timeout = command_timeout
        self._pools: dict[str, asyncpg.Pool | None] = {n: None for n in self.db_names}
        self._pool_locks: dict[str, asyncio.Lock] = {n: asyncio.Lock() for n in self.db_names}
        # Антиспам: error «не удалось восстановить серию» логируем один раз до успеха — ПО
        # КАЖДОМУ пулу отдельно. Общий флаг на объект вёл себя ровно наоборот задуманному:
        # успех ЛЮБОГО пула снимал подавление, поэтому в самом частом сценарии (один пул жив,
        # другой лёг) антиспам не работал вовсе — каждая исчерпанная серия мёртвого пула снова
        # писала error. И наоборот: выставленный из-за одного пула флаг проглатывал ПЕРВУЮ
        # ошибку второго, то есть прятал начало второй поломки.
        self._recovery_error_logged: set[str] = set()

    async def _connect_pool(self, name: str, retries: int = 5, delay: float = 2.0):
        db_name = self.db_names[name]
        for attempt in range(1, retries + 1):
            try:
                # asyncpg.create_pool() возвращает PoolAcquireContext (awaitable через
                # __await__), а не coroutine — без cast статический анализ этого не видит.
                pool_factory = cast(Awaitable[asyncpg.Pool], asyncpg.create_pool(
                    **self._dsn, database=db_name,
                    min_size=self.min_size, max_size=self.max_size,
                    statement_cache_size=0,   # обязательно для PgBouncer transaction mode
                    timeout=10,               # таймаут установки коннекта (TCP/login) — не виснуть на полумёртвом PgBouncer
                    command_timeout=self.command_timeout,
                    init=self._init,
                ))
                self._pools[name] = await pool_factory
                _logger.info(_msg('pool_created', name=name, db_name=db_name,
                                  min_size=self.min_size, max_size=self.max_size))
                return
            except CONNECT_RETRYABLE as error:
                _logger.warning(_msg('pool_attempt', attempt=attempt, retries=retries,
                                     name=name, error=error))
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                else:
                    _logger.error(_msg('pool_create_failed', name=name))
                    raise

    async def connect(self, retries: int = 5, delay: float = 2.0, names=None):
        """Поднять пулы. Идемпотентно (уже поднятый не пересоздаём — иначе утечка).

        `names` — какие именно поднимать; по умолчанию все. Нужен там, где программа
        различает пулы по критичности: один обязателен на старте, другой терпит отказ и
        поднимется лениво при первом запросе (см. _ensure_pool). Без этого пришлось бы
        считать фатальным отказ ЛЮБОГО пула.

        При сбое закрываем поднятое в этом вызове перед пробросом — не оставляем висящие
        соединения к PgBouncer. Пулы, поднятые РАНЬШЕ, не трогаем: иначе неудачная попытка
        поднять второстепенный пул убила бы уже работающий основной."""
        started = []
        try:
            for name in (names if names is not None else self.db_names):
                if self._pools[name] is not None:
                    continue
                await self._connect_pool(name, retries=retries, delay=delay)
                started.append(name)
        except (Exception,):
            for name in started:
                pool = self._pools.get(name)
                if pool is not None:
                    try:
                        await pool.close()
                    except (Exception,):
                        pass
                    self._pools[name] = None
            raise

    async def close(self):
        for name, pool in list(self._pools.items()):
            if pool is not None:
                try:
                    await pool.close()
                    _logger.info(_msg('pool_closed', name=name))
                except (Exception,) as error:
                    _logger.warning(_msg('pool_close_error', name=name, error=error))
                self._pools[name] = None

    async def _ensure_pool(self, name: str):
        """Ленивая (пере)инициализация одного пула под локом. Нужна для авто-восстановления:
        после неудачного `_recreate_pool` пул остаётся None, и без этого следующий запрос
        вечно возвращал бы False (пути назад к connect нет). Одна попытка — не виснуть на
        горячем пути; не вышло → запрос вернёт False, следующий повторит."""
        if self._pools.get(name) is None:
            async with self._pool_locks[name]:
                if self._pools.get(name) is None:
                    await self._connect_pool(name, retries=1)

    async def _recreate_pool(self, name: str):
        async with self._pool_locks[name]:
            pool = self._pools[name]
            if pool is not None:
                try:
                    async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                        # RW-aware health-check: «SELECT 1» проходит и на RO-реплике, поэтому
                        # после Patroni failover пул мог «пройти» проверку и НЕ пересоздаться →
                        # залип бы на read-only-strand. Проверяем именно writable (бэкенд НЕ в
                        # recovery); RO — не скипаем, идём пересоздавать.
                        writable = await conn.fetchval("SELECT NOT pg_is_in_recovery()")
                    if writable:
                        return
                except (Exception,):
                    pass
            try:
                if pool is not None:
                    await pool.close()
            except (Exception,):
                pass
            self._pools[name] = None
            _logger.warning(_msg('pool_recreating', name=name))
            # Одна попытка (не 5 дефолтных): recreate идёт ПОСЛЕ исчерпанных ретраев
            # execute_query и держит _pool_lock — длинный backoff застопорил бы горячий путь.
            await self._connect_pool(name, retries=1)

    async def ensure_pool(self, db: str = "program") -> bool:
        """Поднять пул, если он ещё не поднят, и сказать, готов ли он.

        Публичная проверка для тех, кто идёт за соединением через acquire(): тот требует
        поднятого пула, но синхронный и поднять его не может. execute_query делает то же
        самое внутри себя, поэтому ему такой проверки не нужно."""
        await self._ensure_pool(db)
        return self._pools.get(db) is not None

    def acquire(self, db: str = "program", timeout: int = _ACQUIRE_TIMEOUT):
        """Соединение из пула с таймаутом ожидания свободного коннекта (не зависать, если пул
        деградировал/исчерпан). Для прямых fetch ВНЕ SQL API — например ИИ-боты со своими
        запросами. Возвращает PoolAcquireContext (async with).

        Пул должен быть поднят: проверка на стороне вызывающего — `await ensure_pool(db)`.
        Обёртки ретраев здесь НЕТ — кто берёт соединение напрямую, тот сам отвечает за
        обработку обрыва.

        Две РАЗНЫЕ беды, и типы у них разные:

        * имени нет в конфиге — опечатка в коде, чинится правкой вызова, не ретраем: KeyError,
          как у любого обращения к словарю по несуществующему ключу;
        * пул известен, но не поднят (после неудачного _recreate_pool; между ensure_pool у
          вызывающего и этим вызовом есть окно) — состояние рантайма: ConnectionError.

        Раньше второй случай давал невнятный AttributeError ('NoneType' object has no
        attribute 'acquire'), который долетал до фоновой задачи и уходил в канал как
        загадочный сбой. Правка 0.3.2 назвала его своим типом, но заодно проглотила первый:
        `.get()` отдаёт None и на неизвестное имя — опечатка начала маскироваться под
        «пул не поднят». Поймано тестом test_acquire_uses_named_pool."""
        if db not in self._pools:
            raise KeyError(f"Нет пула '{db}': известные — {', '.join(sorted(self._pools))}")
        pool = self._pools[db]
        if pool is None:
            raise ConnectionError(
                f"Пул '{db}' не поднят (предыдущее пересоздание не удалось) — "
                f"перед acquire() нужен успешный ensure_pool('{db}')")
        return pool.acquire(timeout=timeout)

    async def set_account_premium(self, id_telegram: int, premium: bool) -> bool:
        """Отметка Premium у аккаунта юзербота: `telegram.telegram.premium` (БД Program).

        Единственный конкретный SQL в этом слое, и это осознанно: схема у семьи общая, а
        альтернатива — семь одинаковых методов в семи `database/database.py`. Зовётся из
        `binocore.tg.PremiumGuard` через колбэк `mark`, то есть ставит И снимает отметку —
        таблица отражает факт, а не историю ручных правок.

        Контракт как у execute_query: True — записано, False — сбой (решает вызывающий)."""
        return await self.execute_query(
            "UPDATE telegram.telegram SET premium = $2 WHERE id_telegram = $1",
            id_telegram, premium, fetch_mode="execute", func="set_account_premium", db="program",
        )

    async def execute_query(self, sql: str, *args, retries: int = 3, delay: float = 2.0,
                            fetch_mode: str = "all", func: str = "",
                            db: str = "program"):
        """Контракт: при ОШИБКЕ → False (нет пула / retry exhaust / неизвестный
        fetch_mode / непредвиденная). При успехе — результат: list ('all'),
        Record|None ('row'), значение|None ('val'), True ('execute'). None из
        'row'/'val' = «строки нет», False = «сбой» (их можно различать).
        `db` выбирает пул по ключу из `db_names`.

        Восстановимую ошибку сначала ретраим (та же мёртвая connection в PgBouncer
        transaction-mode обычно лечится следующим acquire), и только после исчерпания
        ретраев пересоздаём пул (health-checked) — не лавина close()+connect() на каждой
        ошибке. `_ensure_pool` поднимает пул, если он None (в т.ч. после проваленного
        recreate) — иначе запрос навсегда отдавал бы False.

        Задержка между попытками — экспонента с джиттером: delay*2^(n-1) + до 40% сверху."""
        for attempt in range(1, retries + 1):
            recoverable_err = None
            try:
                await self._ensure_pool(db)
                pool = self._pools.get(db)
                if pool is None:
                    _logger.error(_msg('pool_missing', db=db, func=func))
                    return False
                async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                    if fetch_mode == "row":
                        res = await conn.fetchrow(sql, *args)
                    elif fetch_mode == "val":
                        res = await conn.fetchval(sql, *args)
                    elif fetch_mode == "all":
                        res = await conn.fetch(sql, *args)
                    elif fetch_mode == "execute":
                        await conn.execute(sql, *args)
                        self._recovery_error_logged.discard(db)
                        return True
                    else:
                        _logger.error(_msg('bad_fetch_mode', fetch_mode=fetch_mode))
                        return False
                    self._recovery_error_logged.discard(db)
                    return res
            except (InterfaceError, CannotConnectNowError, ConnectionDoesNotExistError,
                    ReadOnlySQLTransactionError,
                    ConnectionError, OSError, TimeoutError, asyncio.TimeoutError) as error:
                # ConnectionError/OSError ловят встроенный ConnectionError('unexpected
                # connection_lost() call') из asyncpg — без них он провалился бы в общий
                # except и вернул False без восстановления.
                recoverable_err = error
            except (Exception,) as error:
                msg = str(error)
                if any(m in msg for m in _PGBOUNCER_RECOVERABLE):
                    recoverable_err = error
                else:
                    # Непредвиденное (вероятно баг в SQL/параметрах, не сбой БД) — контракт
                    # обязывает вернуть False, но стек НЕ теряем (иначе реальные баги невидимы).
                    _logger.error(_msg('unexpected_sql', func=func, db=db, msg=msg), exc_info=True)
                    return False

            # сюда — только при восстановимой ошибке (иначе уже вернули результат/False)
            _logger.warning(_msg('connection_dropped', db=db, func=func, attempt=attempt,
                                 retries=retries, error=recoverable_err))
            if attempt < retries:
                backoff = delay * (2 ** (attempt - 1))
                await asyncio.sleep(backoff + random.uniform(0, 0.4 * backoff))
                continue
            # Ретраи исчерпаны — пересоздаём пул для следующих запросов, эту серию валим.
            # warning, а не error: это ДЕЙСТВИЕ, а не итог, и в канал ошибок ему незачем. Итог
            # для оператора рядом — 'restore_failed' (error, под антиспамом), неудача самого
            # пересоздания — 'recreate_failed' (error), а каждая попытка уже записана warning'ом
            # ('connection_dropped'). Прежний error дублировал сигнал и, умноженный на десятки
            # юнитов семьи, забивал общую тему на каждой исчерпанной серии.
            _logger.warning(_msg('recreate_after_retries', func=func, db=db))
            try:
                await self._recreate_pool(db)
            except (Exception,) as pool_error:
                _logger.error(_msg('recreate_failed', db=db, error=pool_error))
            if db not in self._recovery_error_logged:
                _logger.error(_msg('restore_failed', db=db))
                self._recovery_error_logged.add(db)
            return False
        return False
