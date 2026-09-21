"""Слой binodex: вход на binodex.app по одноразовому коду с почты (email-OTP).

Зачем в ядре. Логин жил двенадцатью копиями `apps/otc_login.py`, и 18-09-2026 это вышло боком:
binodex раскатал СВОЮ модалку входа вместо виджета Privy, письмо с кодом начал слать сам
(`account@mail.binodex.io`, код в ТЕМЕ письма) и кладёт в localStorage другой признак сессии —
после чего релогин перестал работать разом у всех. Одну и ту же правку пришлось бы вносить
двенадцать раз; здесь она вносится один раз и раскладывается `sync.py`.

Оба механизма живут ОДНОВРЕМЕННО: что покажут — решает флаг `my.ownAuth` из
`api.binodex.app/config`, и он НЕ по стране (18-09-2026 две ноды в PL дали разные значения, две
в DE тоже — то есть раскат идёт по A/B и переключиться может на любой ноде в любой момент).
Поэтому модуль всюду держит ОБА пути: два вида отправителя письма, код и в теме и в теле, два
ключа сессии, два набора селекторов (они приходят из `settings.binodex_settings`).

Работает сразу после раскладки. Ни один параметр-зависимость не обязателен: нет своего
`goto`/`eval_js`/логгера — берутся дефолты ядра (`default_goto`, `default_eval_js`,
`logging.getLogger`). Программе достаточно разложить ядро скриптом и позвать `inline_login` с
селекторами из БД. Передавать своё стоит там, где у программы уже есть проверенная обёртка: у
копий они разные (`goto_retry` против `goto_with_retry`, `eval_js` с `cap=` против `timeout=`),
и терять их ретраи и префиксы в логе незачем.

Чего здесь нет намеренно. Playwright не импортируется: `page`/`context` берутся уткой. Так
модуль ставится и тестируется без браузера, а слой не привязан к версии Playwright программы.

Настройки читаются из `settings.binodex_settings` (словарь `sel`), а не из кода: адрес
отправителя и разметку меняет binodex, и гонять ради этого раскатку по флоту незачем.
Константы ниже — дефолт на случай старой БД без нужных строк.
"""
import asyncio
import email
import imaplib
import logging
import re
import time
from collections.abc import Mapping
from email.header import decode_header, make_header
from functools import partial

# Отправители письма с кодом: Privy (no-reply@privy.io, no-reply@mail.privy.io) и сам binodex
# (account@mail.binodex.io). Фильтр по домену, потому что адрес внутри домена они уже меняли.
MAIL_FROM = ('privy.io', 'binodex.io')
# Подстрока темы — отсекает прочую почту тех же отправителей. Подходит обоим письмам:
# «Your login code for BinoDex» (Privy) и «Your BinoDex sign-in code: 123456» (binodex).
MAIL_SUBJECT_HINT = 'code'
# Ключи localStorage, по любому из которых сессия считается живой: privy:token — вход через
# Privy, ownAuthSession — собственная авторизация binodex (рядом с ней кладётся accessToken).
SESSION_KEYS = ('privy:token', 'ownAuthSession')
# Проба сессии для evaluate: принимает список ключей, возвращает True, если хоть один на месте.
SESSION_PROBE_JS = 'keys => keys.some(k => !!localStorage.getItem(k))'

CODE_WAIT_SECONDS = 120
CODE_POLL_EVERY = 3
IMAP_OP_TIMEOUT = 30   # сек на одну IMAP-операцию в потоке (connect-таймаут покрывает лишь connect)

# КОНТРАКТ потока ожидания кода: внешний потолок = CODE_WAIT_SECONDS + IMAP_OP_TIMEOUT, то есть
# ОДНА операция, начатая до дедлайна, имеет право на IMAP_OP_TIMEOUT сверху. Держат его две
# вещи: укороченный сокет-таймаут реконнекта (три операции укладываются в одну, см. ниже) и
# проверки дедлайна ВНУТРИ опроса (`wait_for_code` / `code_uids`). Без вторых опрос, начатый на
# 119-й секунде, успевал сделать noop + поиск НА КАЖДОГО отправителя + чтение письма — четыре
# сокет-таймаута подряд, ~80с при разрешённых 30. Ломалось это ровно так же, как обрыв до 0.6.3:
# `wait_for` снимал ожидание, вход объявлялся неудачным (следующий круг просит новый одноразовый
# код и двигает лимит binodex), а поток-сирота доживал на сокет-таймаутах и задерживал остановку
# процесса — `imap.*` паузы не спрашивает и про `stop_wait` не знает.
# Оговорка: сокет-таймаут стоит на ЧТЕНИИ, а не на команде целиком, поэтому сервер, дробящий
# ответ, растянуть её всё же может. Тот отказ, который мы видели (сокет открыт, ответа нет),
# это закрывает; от дробящего спасает только внешний потолок.

# Сколько раз переподключаемся к Gmail ВНУТРИ одного ожидания кода. Gmail роняет IMAP-сессию
# транзиентно — imaplib отдаёт это как `abort: command: NOOP => System Error`, и объект
# соединения после такого мёртв. Без переподключения обрыв рвёт ВЕСЬ вход: письмо с кодом в
# ящик приходит штатно, а мы уже ушли в «релогин не удался» и жжём следующую попытку (а с ней
# и новый одноразовый код — то есть лимит запросов). Число небольшое: дольше держит общий
# дедлайн CODE_WAIT_SECONDS, а не счётчик.
IMAP_RECONNECT_MAX = 3

# Сокет-таймаут ОБЫЧНОГО подключения к ящику (звучит один раз, до входа — спешить некуда).
IMAP_SOCKET_TIMEOUT = 20
# Сокет-таймаут подключения, которое делается ПОСРЕДИ ожидания кода. Внешний потолок потока —
# `CODE_WAIT_SECONDS + IMAP_OP_TIMEOUT`, то есть контракт такой: ОДНА операция, начатая до
# дедлайна, имеет право на IMAP_OP_TIMEOUT сверху. Реконнект — это ТРИ операции (connect,
# login, select), и с обычными 20с он выбивал бы из контракта: обрыв на 119-й секунде тянул
# хвост до ~182с при потолке 150, после чего `wait_for` снимал ожидание, вход объявлялся
# неудачным (ровно то, от чего заводился реконнект), а поток-сирота ещё жил на своих сокет-
# таймаутах. Треть общего потолка на операцию — и весь реконнект укладывается в него целиком.
IMAP_RECONNECT_SOCKET_TIMEOUT = IMAP_OP_TIMEOUT // 3
# Во сколько времени заведомо укладывается реконнект: пауза перед ним плюс три операции под
# потолком выше. Если до дедлайна осталось меньше — не начинаем вовсе: смысл реконнекта в том,
# чтобы ДОЖДАТЬСЯ кода, а не в том, чтобы упереться в потолок уже после него.
RECONNECT_COST = CODE_POLL_EVERY + IMAP_OP_TIMEOUT

REQUIRED_SELECTORS = ('login_open', 'login_email', 'login_submit', 'login_code_inputs')

# Отказ binodex по ЛИМИТУ запросов кода. Отдельный класс, потому что лечение у него обратное
# обычному: повторять вход НЕЛЬЗЯ — каждая попытка продлевает лимит. 18-09-2026 ForumTradeEnglish
# так просила код каждые ~2.5 минуты три часа подряд, 19-09 в ту же яму сползли 3m/5m OTC.
RATE_LIMIT_MARKERS = ('too many requests', 'слишком много запросов', 'try again later',
                      'попробуйте позже')
RATE_LIMIT_PAUSE = 900   # сек тишины после такого отказа (лимит binodex остывает ~25 мин)
RATE_LIMIT_STEP = 1.0    # шаг проверки остановки внутри паузы (см. rate_limit_sleep)


class LoginRateLimited(RuntimeError):
    """binodex отказал по лимиту запросов кода — входить сейчас нельзя, нужна пауза."""


# Ротация сессии. Собственная авторизация binodex держит сессию на ОДНОРАЗОВОМ refresh-токене:
# живой фронт меняет его запросом ниже примерно раз в 15 минут (столько живёт accessToken), а
# сервер помнит потраченный экземпляр и на повтор отвечает
# `401 {"code":"REFRESH_REUSED","message":"refresh token was already used"}`.
# Значит снимок, снятый при входе, устаревает не по времени, а при ПЕРВОЙ ЖЕ ротации: замер
# 19-09-2026 — снимок 674 возрастом 26 часов не поднялся именно с этим кодом, хотя
# `refreshExpiresAt` у него стоял на +30 суток. Отсюда правило: перечитывать storage_state
# после каждой ротации (у Privy такой беды не было — там снимок восстанавливаемый).
SESSION_REFRESH_HINT = '/id/refresh'   # хвост URL обновления сессии (api.binodex.app/v1/id/refresh)
SNAPSHOT_MIN_GAP = 60                  # сек между сохранениями снимка: ответы могут идти пачкой

URL_LANDING = 'https://binodex.app/'
URL_TRADE = 'https://binodex.app/trade'


GOTO_TIMEOUT = 30000   # мс на попытку навигации: на логине страница лёгкая
GOTO_ATTEMPTS = 3      # попыток goto — транзиентный обрыв навигации у binodex обычное дело
GOTO_PAUSE = 1.5       # сек между попытками
EVAL_TIMEOUT = 15      # сек на evaluate внутри логина
CLEAR_COOKIES_TIMEOUT = 15   # сек на context.clear_cookies: CDP-вызов, своего потолка НЕ имеет
LOGOUT_TIMEOUT = 15          # сек на IMAP-logout в finally

# Потолки шагов модалки (мс). Именованные, а не литералы по месту: их складывает LOGIN_BUDGET,
# и при правке потолка бюджет обязан ехать следом сам.
CLICK_TIMEOUT = 15000        # клик «войти» и ввод e-mail
CODE_INPUTS_FAST = 8000      # быстрая проба: ячейки кода после Enter
CODE_INPUTS_SLOW = 15000     # длинная проба: после клика по кнопке отправки
SESSION_WAIT = 30000         # ожидание признака сессии в localStorage
CELL_FILL_TIMEOUT = 30000    # запасной путь ввода кода: до шести fill по таймауту страницы

# Принятый код binodex уводит на /trade САМ, и наш goto поверх этого редиректа гасил сессию
# (см. _wait_own_redirect). Числа сняты с живого аккаунта 19-09-2026: собственный редирект
# приходит примерно за секунду, а сорванная сессия исчезает в первые секунды после загрузки.
OWN_REDIRECT_WAIT = 15       # сек ожидания собственного редиректа binodex на /trade
OWN_REDIRECT_POLL = 0.5      # сек между опросами page.url
SESSION_SETTLE = 5           # сек «отстоя» на /trade перед контрольным чтением признака сессии

# Сбои навигации, которые лечатся повтором. Список собран на живом флоте: первым идёт гонка
# редиректа (фронт сам уводит страницу на загрузке, и Firefox рвёт навигацию), дальше сетевые и
# CDN-сбои. Всё остальное повторять смысла нет — отдаём наверх сразу.
RETRYABLE_GOTO_ERRORS = (
    'NS_BINDING_ABORTED',                # редирект на загрузке → Firefox рвёт навигацию
    'NS_ERROR_FAILURE',                  # общий сетевой сбой (ловился перед binodex.app/Cloudflare)
    'NS_ERROR_NET_RESET',
    'NS_ERROR_NET_TIMEOUT',
    'NS_ERROR_NET_INTERRUPT',
    'NS_ERROR_CONNECTION_REFUSED',
    'NS_ERROR_PROXY_CONNECTION_REFUSED',
    'NS_ERROR_UNKNOWN_HOST',             # транзиентный сбой DNS
    'ERR_CONNECTION_RESET',              # то же самое в Chromium
    'ERR_CONNECTION_CLOSED',
    'ERR_NAME_NOT_RESOLVED',
    'ERR_ABORTED',
    'Timeout',                           # таймаут самого goto (домен не ответил за timeout)
)

def login_budget() -> float:
    """Худший случай всего входа, секунды: СУММА собственных потолков модуля.

    Нужен вызывающему: у `inline_login` внешнего сторожа может не быть, а внутри есть шаги, чей
    потолок задаёт браузер (fill ячеек идёт по таймауту страницы). Считается ЗДЕСЬ, а не в
    программах: иначе правка любого потолка в ядре молча расходится с числом, которое программа
    держит у себя (так и вышло в BinodexScreens 18-09-2026 — выражение на литералах повторяло
    арифметику ядра). Функция, а не константа: значения потолков можно поднять в рантайме, и
    бюджет обязан ехать следом."""
    goto_leg = GOTO_ATTEMPTS * GOTO_TIMEOUT / 1000 + (GOTO_ATTEMPTS - 1) * GOTO_PAUSE
    return (
        2 * IMAP_OP_TIMEOUT                                   # connect + baseline uid
        + 2 * goto_leg                                        # два goto лендинга
        + CLEAR_COOKIES_TIMEOUT + 2 * EVAL_TIMEOUT            # чистка сессии
        + 2 * CLICK_TIMEOUT / 1000                            # клик «войти» + ввод e-mail
        + (CODE_INPUTS_FAST * 2 + CODE_INPUTS_SLOW) / 1000    # проба ячеек → submit → проба
        + CODE_WAIT_SECONDS + IMAP_OP_TIMEOUT                 # код с почты
        + 6 * CELL_FILL_TIMEOUT / 1000                        # запасной ввод по ячейкам
        + SESSION_WAIT / 1000                                 # признак сессии в localStorage
        + OWN_REDIRECT_WAIT                                   # ждём свой редирект binodex
        + goto_leg                                            # goto /trade (запасной путь)
        + SESSION_SETTLE                                      # отстой + контрольное чтение
        + IMAP_OP_TIMEOUT + LOGOUT_TIMEOUT                    # уборка писем + logout
    )


_log = logging.getLogger('binocore.binodex')


class LoginInterrupted(Exception):
    """Вход прерван остановкой процесса, а не сбоем. Разница важна: ложное «релогин не удался»
    и врёт в журнале, и зря тратит попытку счётчика."""


# ── хелперы по умолчанию ──────────────────────────────────────────────────────────────────────
# Модуль работает БЕЗ настройки: разложили ядро скриптом — логин поехал. У программ семьи есть
# свои обёртки над goto и evaluate (с ретраями, потолками и своим префиксом в логе), и они
# по-прежнему передаются параметрами; но там, где их нет, ядро не должно требовать их написать.
async def default_goto(page, url: str, log=None) -> None:
    """page.goto с повторами на ТРАНЗИЕНТНЫХ сбоях навигации. Прочие ошибки — сразу наверх:
    ретраить, скажем, битый URL бессмысленно, а три попытки съедят время перед ожиданием кода.

    `log` — логгер программы: без него повторы ушли бы в безымянный логгер ядра, то есть мимо
    файловых логов программы, и в журнале осталась бы дыра ровно в интересный момент."""
    log = log or _log
    last = None
    for attempt in range(1, GOTO_ATTEMPTS + 1):
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=GOTO_TIMEOUT)
            return
        except (Exception,) as err:
            if not any(mark in str(err) for mark in RETRYABLE_GOTO_ERRORS):
                raise
            last = err
            log.warning(f'binodex: goto {url} — транзиентный сбой ({attempt}/{GOTO_ATTEMPTS}): '
                        f'{str(err).splitlines()[0]}')
            if attempt < GOTO_ATTEMPTS:
                await asyncio.sleep(GOTO_PAUSE)
    raise last


async def default_eval_js(page, js: str, *args):
    """page.evaluate под потолком: у evaluate своего таймаута нет, и зависший рендерер иначе
    подвесил бы логин навсегда."""
    return await asyncio.wait_for(page.evaluate(js, *args), timeout=EVAL_TIMEOUT)


# ── настройки из binodex_settings ─────────────────────────────────────────────────────────────
def mail_froms(sel: dict) -> tuple[str, ...]:
    """Домены отправителей кода (`login_mail_from`, через запятую). Пусто → дефолт MAIL_FROM."""
    raw = (sel.get('login_mail_from') or '').strip()
    froms = tuple(d.strip().lower() for d in raw.split(',') if d.strip())
    return froms or MAIL_FROM


def subject_hint(sel: dict) -> str:
    """Подстрока темы письма с кодом (`login_mail_subject`), в нижнем регистре."""
    return (sel.get('login_mail_subject') or MAIL_SUBJECT_HINT).strip().lower()


def session_keys(sel: dict) -> tuple[str, ...]:
    """Ключи localStorage — признаки живой сессии (`session_keys`, через запятую).

    Нужны не только логину: тем же списком программа проверяет сессию в рантайме (редирект с
    /trade, уход в Demo), поэтому список один на оба сюжета и лежит в БД."""
    raw = (sel.get('session_keys') or '').strip()
    keys = tuple(k.strip() for k in raw.split(',') if k.strip())
    return keys or SESSION_KEYS


# ── IMAP / код из письма (sync — звать через imap_thread: to_thread + потолок) ────────────────
def imap_connect(mail: str, app_pass: str, timeout: int = IMAP_SOCKET_TIMEOUT) -> imaplib.IMAP4_SSL:
    """Подключиться к ящику. `timeout` — сокет-таймаут КАЖДОЙ из трёх операций (connect, login,
    select), поэтому подключение целиком стоит до 3×timeout. Реконнект посреди ожидания кода
    зовёт с укороченным (см. IMAP_RECONNECT_SOCKET_TIMEOUT), чтобы уложиться в потолок потока."""
    imap = imaplib.IMAP4_SSL('imap.gmail.com', 993, timeout=timeout)
    imap.login(mail, app_pass)
    imap.select('INBOX')
    return imap


def code_uids(imap, froms: tuple[str, ...] = MAIL_FROM, deadline: float | None = None) -> list[int]:
    """UID писем от ЛЮБОГО из отправителей кода. Поиск идёт отдельным запросом на домен, а не
    одним «OR ...»: вложенный OR разные IMAP-серверы разбирают по-своему, а лишний round-trip
    здесь ничего не стоит (в ящике единицы писем).

    `deadline` (monotonic) — не начинать ОЧЕРЕДНОЙ поиск, если окно ожидания кода уже вышло:
    каждый поиск на молчащем сокете стоит свой таймаут, и на двух отправителях это удваивало
    хвост потока (см. про контракт у IMAP_OP_TIMEOUT). Отданное частично — не потеря: за
    дедлайном вызывающий всё равно уходит с ошибкой."""
    uids: set[int] = set()
    for sender in froms:
        if deadline is not None and time.monotonic() >= deadline:
            break
        # noinspection PyTypeChecker
        _, data = imap.uid('search', None, f'(FROM "{sender}")')  # None — charset (валидно для IMAP)
        if data and data[0]:
            uids.update(int(x) for x in data[0].split())
    return sorted(uids)


def extract_code(imap, uid: int, hint: str = MAIL_SUBJECT_HINT) -> str | None:
    """Шестизначный код из письма или None, если письмо не про вход.

    Тема читается ПЕРВОЙ: у собственной авторизации binodex код стоит прямо в ней, и тело тогда
    разбирать незачем. У Privy тема без кода — код лежит в теле, как было раньше."""
    _, md = imap.uid('fetch', str(uid), '(RFC822)')
    if not md or not md[0]:
        return None
    msg = email.message_from_bytes(md[0][1])
    subject = str(make_header(decode_header(msg.get('Subject', ''))))
    if hint not in subject.lower():
        return None
    match = re.search(r'\b(\d{6})\b', subject)
    if match:
        return match.group(1)
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() in ('text/plain', 'text/html'):
            body = part.get_payload(decode=True)
            if not body:
                continue
            try:
                txt = body.decode(part.get_content_charset() or 'utf-8', 'ignore')
            except (Exception,):
                continue
            match = re.search(r'\b(\d{6})\b', txt)
            if match:
                return match.group(1)
    return None


def wait_for_code(imap, baseline: set[int], froms: tuple[str, ...] = MAIL_FROM,
                  hint: str = MAIL_SUBJECT_HINT, stop_wait=None,
                  reconnect=None, logger=None) -> tuple[str, object]:
    """Первое письмо с кодом ПОСЛЕ запроса (uid не из baseline) — старые коды игнорируем.
    Блокирующий поллинг IMAP до CODE_WAIT_SECONDS — звать через imap_thread.

    Возвращает (код, СОЕДИНЕНИЕ): внутри ожидания соединение может быть ПЕРЕСОЗДАНО, поэтому
    вызывающий обязан работать дальше с возвращённым объектом — им же чистятся письма и
    делается logout. Старый после обрыва непригоден, и purge по нему тихо не сделал бы ничего.

    `stop_wait(seconds) -> bool` — пауза между опросами, которая умеет прерваться по остановке
    процесса (True = пора уходить). Программе это не роскошь: отмена таска по SIGTERM
    освобождает async-сторону мгновенно, а ЭТОТ поток живёт дальше, и asyncio.run на выходе
    ждёт потоки пула без таймаута (3.11) — сигнал, пришедший в окно ожидания кода, держал
    процесс до двух минут уже после teardown, что при TimeoutStopSec=120 означало SIGKILL.
    Без параметра поведение прежнее: обычный time.sleep.

    `reconnect() -> imap` — как поднять ЗАНОВО соединение с ящиком (обычно
    `partial(imap_connect, mail, app_pass, IMAP_RECONNECT_SOCKET_TIMEOUT)`). Без него первый же
    транзиентный обрыв Gmail уходит наверх и рвёт вход, хотя письмо с кодом в ящике штатное.
    UID после переподключения остаются теми же (UIDVALIDITY ящика не меняется), поэтому
    baseline продолжает работать. В последние RECONNECT_COST секунд окна реконнект НЕ
    начинается: он всё равно не успел бы дождаться кода, а поток вылез бы за внешний потолок.

    Дедлайн сверяется не только между опросами, но и ВНУТРИ опроса (перед каждым поиском и
    после чтения письма) — чтобы за окно выходила максимум одна операция, как обещает контракт
    у IMAP_OP_TIMEOUT."""
    pause = stop_wait or (lambda seconds: bool(time.sleep(seconds)))
    log = logger or _log
    deadline = time.monotonic() + CODE_WAIT_SECONDS
    reconnects = 0
    # Соединение, открытое ВНУТРИ ожидания. Наверх оно уходит только через `return`, а на
    # ветках отказа вызывающий держит СТАРЫЙ объект и гасит в своём finally именно его —
    # свежий сокет дожил бы до таймаута на каждом провале. Поэтому гасим его сами (finally).
    owned = None
    delivered = False
    try:
        while time.monotonic() < deadline:
            try:
                imap.noop()
                # Дедлайн сверяем и МЕЖДУ операциями опроса, не только между опросами: иначе
                # опрос, начатый в последнюю секунду окна, тянет четыре сокет-таймаута подряд
                # и выводит поток за внешний потолок (см. про контракт у IMAP_OP_TIMEOUT).
                for uid in sorted(set(code_uids(imap, froms, deadline)) - baseline, reverse=True):
                    # Чтение письма НЕ пропускаем по дедлайну: это операция-развязка, ради
                    # которой всё и затевалось, а свежий uid уже найден. Ограничиваем число
                    # таких чтений — после первого за дедлайном уходим.
                    code = extract_code(imap, uid, hint)
                    if code:
                        delivered = True
                        return code, imap
                    if time.monotonic() >= deadline:
                        break
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as err:
                # Транзиент Gmail: соединение мертво, но код в ящик придёт (или уже пришёл).
                # Пересоздаём сессию и ждём дальше в пределах ТОГО ЖЕ дедлайна — счётчик попыток
                # нужен лишь против бесконечного цикла на ящике, который не поднимается вовсе.
                if (reconnect is None or reconnects >= IMAP_RECONNECT_MAX
                        or time.monotonic() + RECONNECT_COST > deadline):
                    raise
                reconnects += 1
                log.warning(f'binodex: IMAP оборвался ({err}) — переподключаюсь '
                            f'({reconnects}/{IMAP_RECONNECT_MAX})')
                safe_logout(imap)          # мёртвое соединение закрываем тихо
                if pause(CODE_POLL_EVERY):
                    raise LoginInterrupted('остановка процесса — ожидание кода прервано')
                imap = reconnect()
                owned = imap
                continue
            if pause(CODE_POLL_EVERY):
                raise LoginInterrupted('остановка процесса — ожидание кода прервано')
        raise RuntimeError(f'код входа не пришёл за {CODE_WAIT_SECONDS}с '
                           f'(искал письма от {", ".join(froms)})')
    finally:
        if owned is not None and not delivered:
            safe_logout(owned)


def purge_code_mail(imap, froms: tuple[str, ...] = MAIL_FROM) -> None:
    """Удалить письма с одноразовыми кодами. Gmail: ярлык \\Trash."""
    uids = code_uids(imap, froms)
    if not uids:
        return
    uid_set = ','.join(str(u) for u in uids)
    for store in (('+X-GM-LABELS', '\\Trash'), ('+FLAGS', '\\Deleted')):
        try:
            imap.uid('STORE', uid_set, *store)
        except (Exception,):
            pass
    try:
        imap.expunge()
    except (Exception,):
        pass


def safe_logout(imap) -> None:
    try:
        imap.logout()
    except (Exception,):
        pass


async def imap_thread(fn, *args, timeout: int = IMAP_OP_TIMEOUT):
    """IMAP-операция в потоке под жёстким потолком: зависший сервер в середине сессии не вешает
    async-флоу навсегда (поток-сирота добьётся сокет-таймаутом). asyncio.to_thread сам не
    отменяем, но await вернётся по таймауту → флоу не залипает."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)


# ── шаги в браузере (page/context — утки, хелперы приходят от программы) ──────────────────────
async def _clear_session(page, context, eval_js) -> None:
    """Сбросить старую (битую) сессию перед логином — чтобы протухшие ключи не путали фронт.
    Логинимся «как с чистого листа», но в том же браузере."""
    try:
        # Потолок обязателен: clear_cookies — CDP-вызов, своего таймаута у него НЕТ, и зависший
        # рендерер иначе подвешивал бы вход навсегда (у релогина внешнего сторожа может не быть).
        await asyncio.wait_for(context.clear_cookies(), timeout=CLEAR_COOKIES_TIMEOUT)
    except (Exception,):
        pass
    try:
        await eval_js(page, '() => { try { localStorage.clear(); sessionStorage.clear(); } catch(e){} }')
    except (Exception,):
        pass


async def _wait_code_inputs(page, selector: str, timeout: int) -> None:
    """Дождаться появления ≥6 полей ввода OTP-кода (в обеих модалках их ровно шесть)."""
    await page.wait_for_function('s => document.querySelectorAll(s).length >= 6',
                                 arg=selector, timeout=timeout)


async def _enter_code(page, selector: str, code: str) -> None:
    cells = page.locator(selector)
    if await cells.count() < 6:
        raise RuntimeError(f'ожидал 6 ячеек кода, нашёл {await cells.count()}')
    await cells.first.click()
    await page.keyboard.type(code, delay=60)            # OTP-виджет сам раскидает цифры
    if await cells.first.input_value() != code[0]:      # фолбэк: по цифре в ячейку
        for i, ch in enumerate(code):
            await cells.nth(i).fill(ch)


async def _alert_text(page, sel: dict, eval_js) -> str:
    """Текст отказа, который модалка показала вместо экрана кода («слишком много запросов»,
    «неверный e-mail»). Пусто — модалка молчит, значит причина не в её ответе.

    Без этого в логе оставался безликий Playwright-таймаут: 18-09-2026 binodex отвечал «too many
    requests» после серии релогинов, а в журнале стояло только «Timeout 15000ms»."""
    selector = sel.get('login_alert')
    if not selector:
        return ''
    try:
        texts = await eval_js(page, "s => Array.from(document.querySelectorAll(s))"
                                    ".map(e => (e.innerText || '').trim()).filter(Boolean)", selector)
    except (Exception,):
        return ''
    return ' / '.join(texts)[:200] if texts else ''


async def _wait_own_redirect(page, is_trade, timeout: float = OWN_REDIRECT_WAIT) -> bool:
    """Дождаться, пока binodex САМ уведёт страницу на /trade. True — увёл, False — не дождались.

    Зачем вообще ждать вместо того, чтобы перейти самим. После принятого кода binodex делает
    свой переход на /trade примерно за секунду, а наш `goto` в тот же момент ложился ПОВЕРХ
    чужого редиректа: навигация рвала инициализацию приложения, и binodex гасил только что
    выданную сессию. Снаружи это неотличимо от протухших кук — логин рапортовал успех, ключа в
    localStorage через секунду уже не было, `init` видел Demo, и программа шла на следующий
    релогин, сжигая одноразовые коды до лимита (19-09-2026: пять «успешных» входов за 90 секунд,
    дальше «Too many requests» и остановка юнита). Воспроизведено дважды на одном аккаунте:
    ручные шаги БЕЗ своего goto дают живую сессию, штатный вход со своим goto — пустой
    localStorage.

    Опрос `page.url`, а не `wait_for_url`: страница здесь утка (модуль не импортирует
    Playwright), и у программ она разных версий."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if is_trade(page.url):
                return True
        except (Exception,):
            pass            # страница в середине навигации — просто спросим ещё раз
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(OWN_REDIRECT_POLL)


async def _session_survived(page, keys, eval_js, logger) -> bool:
    """Пережил ли признак сессии загрузку /trade. Только это и есть доказательство входа.

    Раньше успехом считался САМ ФАКТ появления ключа сразу после кода — а binodex умеет принять
    код, отдать сессию и погасить её на первом же переходе. Поэтому читаем ПОВТОРНО, дав
    странице отстояться.

    Не прочитали (рендерер занят, evaluate упал) — НЕ винить: молчание страницы не доказывает
    отвала, а ложный неуспех стоит одноразового кода. Настоящая вторая проверка всё равно
    впереди: вызывающий смотрит снимок storage_state через `has_session` и без признака сессии
    в БД его не пишет."""
    await asyncio.sleep(SESSION_SETTLE)
    try:
        return bool(await eval_js(page, SESSION_PROBE_JS, list(keys)))
    except (Exception,) as err:
        logger.warning(f'OTC inline-логин: не прочитать признак сессии после загрузки /trade '
                       f'({err}) — вход не оспариваю, решит снимок кук')
        return True


async def rate_limit_sleep(seconds: float = RATE_LIMIT_PAUSE, *, stop=None,
                           step: float = RATE_LIMIT_STEP) -> bool:
    """Пауза после отказа по лимиту кодов, ПРЕРЫВАЕМАЯ остановкой процесса.
    True — выдержали её целиком, False — прервала остановка.

    Голый `asyncio.sleep(RATE_LIMIT_PAUSE)` здесь стоить дорого: 19-09-2026 юнит, стоявший в
    этой паузе, на SIGTERM не начинал уборку вовсе — systemd добивал его SIGKILL по
    `TimeoutStopSec`, и в журнале оставалось `Result=timeout, ExecMainStatus=9`. Для диспетчера
    9/KILL — краш, то есть ещё один повод к рестартам и переносам, ровно к тем, от которых
    пауза и придумана.

    `stop` — что угодно с `.is_set()` (asyncio.Event у одних программ, threading.Event у
    других) либо вызываемое, возвращающее «пора уходить». None — обычный сон (ранний старт,
    события ещё нет). Шаг мелкий: teardown должен начаться в пределах секунды, а не минут."""
    def _stopped() -> bool:
        if stop is None:
            return False
        try:
            probe = getattr(stop, 'is_set', None) or stop
            return bool(probe())
        except (Exception,):
            return False          # нечитаемый признак остановки не повод рвать паузу

    left = max(0.0, float(seconds))
    while left > 0:
        if _stopped():
            return False
        nap = min(step, left)
        await asyncio.sleep(nap)
        left -= nap
    return not _stopped()


def watch_session_refresh(page, on_rotated, *, hint: str = SESSION_REFRESH_HINT, logger=None,
                          min_gap: float = SNAPSHOT_MIN_GAP) -> bool:
    """Звать `on_rotated()` после каждой успешной ротации сессии binodex. True — подписались.

    Зачем. refresh-токен у собственной авторизации binodex ОДНОРАЗОВЫЙ (см. SESSION_REFRESH_HINT):
    как только живой браузер его прокрутил, экземпляр, лежащий в БД, становится потраченным, и
    следующий холодный старт получает 401 REFRESH_REUSED → «куки протухли» → релогин → письмо с
    одноразовым кодом. При серии рестартов (диспетчер делает их сам) это упирается в лимит
    запросов кода. Лечение — держать в БД ТЕКУЩИЙ снимок: подписаться на ответ обновления и
    перезаписывать storage_state сразу после него.

    `on_rotated()` — корутина вызывающего: снять storage_state и сохранить (у программ свои
    таблица, владелец кук и потолок). Её сбой глушим: не сохранили снимок — работа продолжается,
    цена ошибки всего лишь релогин на следующем подъёме.

    Подписка ИДЕМПОТЕНТНА: init зовут на каждом подъёме браузера, а страница между ними может
    быть той же (reload вместо пересоздания) — второй обработчик сохранял бы снимок дважды.
    `page`/`response` — утки: Playwright модуль не импортирует."""
    logger = logger or _log
    if getattr(page, '_binocore_session_watch', False):
        return False
    guard = {'at': 0.0, 'busy': False}

    async def _save() -> None:
        try:
            await on_rotated()
        except (Exception,) as err:
            logger.warning(f'binodex: снимок сессии после ротации не сохранён ({err})')
        finally:
            guard['at'] = time.monotonic()
            guard['busy'] = False

    def _on_response(response) -> None:
        try:
            if hint not in response.url or response.status >= 400:
                return
        except (Exception,):
            return            # ответ уже недоступен (страница ушла) — ротацию пропускаем
        if guard['busy'] or time.monotonic() - guard['at'] < min_gap:
            return
        guard['busy'] = True  # флаг ставим ДО планирования: ответы приходят пачкой
        try:
            asyncio.get_running_loop().create_task(_save())
        except (Exception,) as err:
            guard['busy'] = False
            logger.warning(f'binodex: сохранение снимка после ротации не запущено ({err})')

    page.on('response', _on_response)
    page._binocore_session_watch = True
    return True


def _report(logger, message: str) -> None:
    """Успех входа — в отчётный уровень программы, если он у неё есть (у семьи это `report`)."""
    getattr(logger, 'report', logger.info)(message)


# ── куки сессии ───────────────────────────────────────────────────────────────────────────────
def has_session(state: Mapping[str, object], keys=SESSION_KEYS) -> bool:
    """Есть ли в снимке storage_state признак живой сессии.

    Снимок без него в БД писать нельзя: 18-09-2026 такой и записался — вход прошёл, но binodex
    погасил сессию сразу после него, и вместо рабочих кук в БД легли служебные ключи. Следующий
    подъём начинал с заведомо мёртвого набора, то есть программа своей же рукой портила
    последние живые куки."""
    # Mapping[str, object], а не dict и не голый Mapping. playwright отдаёт из storage_state()
    # тип StorageState — TypedDict, в рантайме обычный dict, но по PEP 589 с `dict` НЕ совместим,
    # и вызывающему пришлось бы оборачивать снимок в dict() ради проверки типов (так и вышло в
    # ForumTradeEnglish 18-09). ПАРАМЕТРЫ обязательны: PEP 589 гарантирует совместимость
    # TypedDict именно с `Mapping[str, object]`, а неаннотированный `Mapping` — это
    # `Mapping[Any, Any]`, и на нём проверяющие типов (PyCharm) ругаются «Expected type
    # 'Mapping', got 'StorageState' instead» прямо на вызове (ForumTrade, otc_app:1331).
    names = {item.get('name') for origin in (state or {}).get('origins', [])
             for item in origin.get('localStorage', [])}
    return bool(names & set(keys))


# ── модалка поверх страницы ───────────────────────────────────────────────────────────────────
# Кнопка закрытия ВНУТРИ модалки, которую binodex поднимает поверх /trade (онбординг, промо,
# анонс). Ищем ТОЛЬКО по служебным признакам закрывашки (aria-label / data-testid / класс со
# словом close) и по значку-крестику — намеренно НЕ по подписям вроде «OK» или «Got it»:
# в промо-модалке такая кнопка ведёт на внешнюю страницу, то есть «закрытие» увело бы бота с
# торговой страницы. Ничего не нашли — вернём пусто, у вызывающего есть свои пути (Escape,
# клик по краю бэкдропа, DOM-событие).
MODAL_CLOSE_JS = """
() => {
  const CROSS = ['\u00d7', '\u2715', '\u2716', '\u2717', '\u2718', 'x'];
  const BY_ATTR = ['[aria-label*="close" i]', '[data-testid*="close" i]',
                   'button[class*="close" i]', '[class*="closeBtn" i]', '[class*="close_btn" i]'];
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight);
  const roots = document.querySelectorAll('[role="presentation"], [role="dialog"], [aria-modal="true"]');
  for (const root of roots) {
    for (const sel of BY_ATTR) {
      for (const btn of root.querySelectorAll(sel)) {
        if (!visible(btn)) continue;
        try { btn.click(); return sel; } catch (e) {}
      }
    }
    for (const btn of root.querySelectorAll('button,[role="button"]')) {
      if (!visible(btn)) continue;
      if (CROSS.includes((btn.innerText || '').trim().toLowerCase())) {
        try { btn.click(); return '\u043a\u0440\u0435\u0441\u0442\u0438\u043a'; } catch (e) {}
      }
    }
  }
  return '';
}
"""


async def close_modal_button(page, *, eval_js=None) -> str:
    """Нажать крестик модалки binodex. Возврат — по какому признаку нашли кнопку ('' — не нашли).

    Нужен потому, что остальные пути лесенки бьют по БЭКДРОПУ, а модалку-анонс binodex рисует
    картинкой поверх него: клик в центр Playwright не пропускает («subtree intercepts pointer
    events»), а закрывается ли такая модалка кликом по бэкдропу — зависит от того, как её
    собрали. Крестик закрывает её честно, и она уходит ИЗ КАДРА, а не только перестаёт мешать
    кликам. Ошибки не критичны: не вышло — вызывающий идёт дальше по своей лесенке."""
    eval_js = eval_js or default_eval_js
    try:
        return await eval_js(page, MODAL_CLOSE_JS) or ''
    except (Exception,):
        return ''


# ── чарт: фоновая подложка ────────────────────────────────────────────────────────────────────
async def chart_bg_on(page, wrap_bg: str | None) -> bool | None:
    """Включена ли фоновая подложка чарта. None — спросить не вышло (нет селектора, страница
    моргнула). Признак прямой: узел `.wrap_bg` ЕСТЬ в DOM = подложка включена; при выключенной
    настройке binodex его не создаёт вовсе."""
    if not wrap_bg:
        return None
    try:
        return bool(await page.locator(wrap_bg).first.count())
    except (Exception,):
        return None


async def apply_chart_background(page, *, settings_btn: str | None, theme_open: str | None,
                                 theme_toggle: str | None, wrap_bg: str | None, logger=None,
                                 click_timeout: int = 5000, dismiss=None,
                                 settle_ms: int = 500) -> None:
    """Выключить фоновую подложку чарта (картинка с быком и медведем) — настройкой аккаунта.

    ЗАЧЕМ. Подложку рисует сам binodex во весь вьюпорт, и она дорогая: замер на живом сайте
    17-09-2026 дал 118.9% CPU с ней против 63.2% и 57.6% без неё — то есть она стоит примерно
    столько же, сколько вся остальная страница. В кадр подписчику она при этом не попадает
    вовсе (кадр берётся из canvas.toDataURL, куда DOM не входит), так что платить за неё нечем.

    ПОЧЕМУ НАСТРОЙКОЙ, А НЕ localStorage. Ключ `isChartBgVisible` действительно лежит в
    localStorage и уезжает в storage_state, но писать его напрямую — второй способ делать то же
    самое: в settings.binodex_settings давно заведены строки `setup_theme`, `setup_theme_toggle`
    и `wrap_bg` под этот самый переключатель.

    ИДЕМПОТЕНТНОСТЬ. Клик по переключателю ТОГГЛИТ, поэтому сначала смотрим состояние: подложки
    нет — в UI не лезем вовсе (один `count()`).

    Путь в UI: шестерёнка (`settings_btn`) → «Theme» (`theme_open`) → переключатель
    (`theme_toggle`). Ошибки не критичны (подложка — оформление страницы, не данные): лог и дальше.
    `dismiss` — колбэк программы, гасящий модалку binodex поверх страницы (её бэкдроп
    перехватывает клики)."""
    logger = logger or _log
    if not (theme_open and theme_toggle):
        return                      # старая БД без строк — просто не выключаем
    if not await chart_bg_on(page, wrap_bg):
        # False — подложка уже выключена, None — спросить не вышло. В обоих случаях в UI не
        # лезем: клик по переключателю ТОГГЛИТ, и вслепую мы бы её включили.
        return
    try:
        if dismiss is not None:
            await dismiss(page)
        await page.locator(settings_btn).first.click(timeout=click_timeout)
        await page.locator(theme_open).first.click(timeout=click_timeout)
        toggle = page.locator(theme_toggle).first
        await toggle.wait_for(state='visible', timeout=click_timeout)
        await toggle.click(timeout=click_timeout)
        await page.wait_for_timeout(settle_ms)
        if await chart_bg_on(page, wrap_bg) is False:
            # info, а не report: это рутина подъёма, а не событие для служебной темы. У наборов
            # кук, где binodex включил подложку сам, строка уходила бы в Telegram на КАЖДОМ
            # холодном старте, а тема общая на весь флот.
            logger.info('OTC: фоновая подложка чарта выключена настройкой аккаунта '
                        '(binodex включил её сам) — снимаю лишнюю нагрузку на CPU')
        else:
            logger.warning('OTC: переключатель темы нажат, но подложка осталась — '
                           'проверь selectors setup_theme/setup_theme_toggle в binodex_settings')
    except (Exception,) as error:
        logger.warning(f'OTC: не удалось выключить подложку чарта: {error}')
    finally:
        # Закрыть меню настроек, иначе оно висит поверх страницы до конца жизни браузера.
        try:
            await page.locator(settings_btn).first.click(timeout=click_timeout)
        except (Exception,):
            pass


async def inline_login(page, context, *, mail: str, app_pass: str, sel: dict,
                       goto=None, eval_js=None, logger=None, on_trade=None, stop_wait=None) -> bool:
    """Залогиниться в binodex.app по email-OTP прямо в текущем (живом) браузере.

    True — вход удался (признак сессии ПЕРЕЖИЛ загрузку /trade). False — любой сбой
    (лог + откат): вызывающий тогда не сохраняет куки и считает попытки сам.

    Шаги: чистим сессию → страница авторизации → login_open → e-mail → код из почты (IMAP) →
    ввод → ждём признак сессии → ЖДЁМ СОБСТВЕННЫЙ редирект binodex на /trade (свой goto —
    только если его не было) → перечитываем признак сессии. Селекторы и URL — из `sel`
    (binodex_settings).

    Все зависимости НЕОБЯЗАТЕЛЬНЫ: без них берутся дефолты ядра, то есть модуль работает сразу
    после раскладки sync.py. Передают их там, где у программы есть своя обёртка (у копий они
    разные — goto_retry против goto_with_retry, eval_js с `cap=` против `timeout=`):
      `goto(page, url)` — навигация с ретраями программы (иначе default_goto);
      `eval_js(page, js, *args)` — evaluate под потолком программы (иначе default_eval_js);
      `logger` — логгер программы (иначе logging.getLogger('binocore.binodex'));
      `on_trade(url) -> bool` — детект торговой страницы; по умолчанию хвост «/trade»;
      `stop_wait(seconds) -> bool` — прерываемая пауза ожидания кода (см. wait_for_code).
    """
    logger = logger or _log
    goto = goto or (lambda pg, link: default_goto(pg, link, log=logger))
    eval_js = eval_js or default_eval_js
    missing = [k for k in REQUIRED_SELECTORS if not sel.get(k)]
    if missing:
        logger.error(f'OTC inline-логин: нет обязательных селекторов {missing}')
        return False
    landing = sel.get('landing_url') or URL_LANDING
    trade = sel.get('trade_url') or URL_TRADE
    froms = mail_froms(sel)
    hint = subject_hint(sel)
    keys = list(session_keys(sel))
    is_trade = on_trade or (lambda url: url.rstrip('/').endswith('/trade'))
    try:
        imap = await imap_thread(imap_connect, mail, app_pass)
    except (Exception,) as err:
        logger.error(f'OTC inline-логин: не подключиться к почте (IMAP) — {err}')
        return False
    imap_timed_out = False
    try:
        baseline = set(await imap_thread(code_uids, imap, froms))   # старые коды — игнор
        await goto(page, landing)
        await _clear_session(page, context, eval_js)
        await goto(page, landing)  # перезагрузка начисто
        await page.click(sel['login_open'], timeout=CLICK_TIMEOUT)
        await page.fill(sel['login_email'], mail, timeout=CLICK_TIMEOUT)
        await page.locator(sel['login_email']).first.press('Enter')  # отправка надёжнее через Enter
        try:
            await _wait_code_inputs(page, sel['login_code_inputs'], CODE_INPUTS_FAST)
        except (Exception,):
            await page.locator(sel['login_submit']).first.click(timeout=CODE_INPUTS_FAST)
            try:
                await _wait_code_inputs(page, sel['login_code_inputs'], CODE_INPUTS_SLOW)
            except (Exception,):
                # Экран кода не открылся. Прежде чем отдать наверх голый таймаут, спросим саму
                # модалку: чаще всего она прямо пишет причину, и это НЕ наша поломка (лимит
                # запросов кода, отвергнутый адрес). Это единственное, что отличает «binodex
                # отказал» от «селекторы протухли».
                alert = await _alert_text(page, sel, eval_js)
                if alert:
                    logger.warning(f'OTC inline-логин: binodex отказал на шаге e-mail — «{alert}»')
                    if any(m in alert.lower() for m in RATE_LIMIT_MARKERS):
                        # Не просто «не вышло»: пока лимит горит, КАЖДЫЙ новый запрос кода его
                        # продлевает. Отдаём отдельным классом — вызывающий выдержит паузу
                        # вместо того, чтобы жечь попытки счётчика (19-09-2026).
                        raise LoginRateLimited(alert)
                    return False
                raise
        # Соединение забираем обратно: внутри ожидания его могли пересоздать после обрыва
        # Gmail, и уборка писем с logout ниже обязаны идти по ЖИВОМУ объекту.
        code, imap = await imap_thread(
            partial(wait_for_code, imap, baseline, froms, hint, stop_wait,
                    partial(imap_connect, mail, app_pass, IMAP_RECONNECT_SOCKET_TIMEOUT), logger),
            timeout=CODE_WAIT_SECONDS + IMAP_OP_TIMEOUT)
        await _enter_code(page, sel['login_code_inputs'], code)
        # Ключ сессии зависит от механизма входа, который binodex выбирает сам — ждём ЛЮБОЙ из
        # списка. Пока ждали именно privy:token, вход по новой модалке проходил, а мы считали
        # его провалом по таймауту и не сохраняли свежие куки (18-09-2026).
        await page.wait_for_function(SESSION_PROBE_JS, arg=keys, timeout=SESSION_WAIT)
        # На /trade нас уводит САМ binodex — свой goto только если он этого не сделал. Наша
        # навигация поверх чужого редиректа гасила выданную сессию (см. _wait_own_redirect).
        if await _wait_own_redirect(page, is_trade):
            logger.info('OTC inline-логин: binodex сам увёл на /trade — свой переход не делаю')
        else:
            logger.info(f'OTC inline-логин: своего редиректа на /trade не дождался за '
                        f'{OWN_REDIRECT_WAIT}с — перехожу сам')
            await goto(page, trade)
        if not is_trade(page.url):
            logger.warning(f'OTC inline-логин: после входа редирект с /trade на {page.url}')
            return False
        # Вход засчитываем ТОЛЬКО тем, что пережило загрузку /trade.
        if not await _session_survived(page, keys, eval_js, logger):
            logger.warning('OTC inline-логин: binodex погасил сессию сразу после входа '
                           '(признак сессии исчез на /trade) — вход НЕ состоялся')
            return False
        # ВСЁ, что ниже, — уборка, а не часть входа: вход уже доказан (признак сессии в
        # localStorage + мы на /trade). Поэтому её сбой НЕ должен превращаться в «релогин не
        # удался»: раньше уборка стояла голой, и подвисший IMAP уводил поток в ветку ошибки
        # ниже → return False → вызывающий не сохранял свежий storage_state в БД → программа
        # останавливалась с «куки не восстановлены», притом что куки восстановлены и лежат в
        # живом контексте. Цена уборки одноразовых кодов — не остановка программы.
        try:
            await imap_thread(purge_code_mail, imap, froms)
        except (TimeoutError, asyncio.TimeoutError) as err:
            # Тот же гонка-опасный случай, что и в ветке ниже: поток-сирота может ещё держать
            # сокет imaplib, а он не thread-safe → logout в finally пропускаем.
            imap_timed_out = True
            logger.warning(f'OTC inline-логин: уборка писем с кодами не уложилась в потолок ({err}) '
                           f'— вход состоялся, письма останутся в ящике')
        except (Exception,) as err:
            logger.warning(f'OTC inline-логин: уборка писем с кодами не удалась ({err}) '
                           f'— вход состоялся, письма останутся в ящике')
        _report(logger, 'OTC: inline-релогин binodex успешен')
        return True
    except LoginRateLimited:
        # Мимо общего except ниже: он превратил бы лимит в безликое «вход не удался», и
        # вызывающий пошёл бы на следующий круг, продлевая лимит собственными запросами.
        raise
    except LoginInterrupted as stop:
        # Нас останавливают — это НЕ сбой логина: ложное «релогин не удался» и врёт в журнале, и
        # зря тратит попытку счётчика. Пишем фактом, уровнем info.
        logger.info(f'OTC inline-логин прерван остановкой процесса: {stop}')
        return False
    except (TimeoutError, asyncio.TimeoutError) as err:
        # imap_thread упёрся в потолок wait_for: поток-сирота, возможно, ещё держит imap (сам
        # умрёт по сокет-таймауту соединения, ≤20с). Звать safe_logout на ТОМ ЖЕ imap из finally
        # нельзя — параллельная работа двух потоков на сокете imaplib (он не thread-safe) даёт гонку.
        imap_timed_out = True
        logger.warning(f'OTC inline-логин: IMAP-операция превысила потолок — {err}')
        return False
    except (Exception,) as err:
        logger.warning(f'OTC inline-логин не удался: {err}')
        return False
    finally:
        # logout пропускаем, если была гонка-опасная отмена по таймауту (см. выше) — мёртвое
        # соединение всё равно закроется, когда поток-сирота добьётся сокет-таймаутом.
        if not imap_timed_out:
            try:
                await imap_thread(safe_logout, imap, timeout=LOGOUT_TIMEOUT)
            except (Exception,):
                pass  # logout под потолком; зависший сервер не должен держать выход из флоу
