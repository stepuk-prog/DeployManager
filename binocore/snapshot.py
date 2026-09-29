"""Ссылка на готовый кадр графика binodex (snapshot-API) — построитель URL и профиль кадра.

Зачем. binodex отдаёт PNG графика по одной ссылке
    https://binodex.app/og/<пара>?key=<apiKey>&tf=30s&indicators=whaleabs,stoch,volume&…
(документ партнёра: «Скриншот графика для партнёров», ред. 25-09-2026). Кадр рисует их фронт в
браузере Cloudflare, цена и время кадра приходят заголовками `X-Snapshot-*`. Для нас это замена
своего браузера на /trade со всем, что на нём держится: вход по коду с почты, модалки, настройка
шкал и индикаторов, селекторы из БД.

Что здесь. Только ПОСТРОЕНИЕ ссылки из профиля кадра (строка `settings.snapshot_profile`,
привязка — `settings.option_setting.snapshot_profile`) и то, что к нему прилагается: имя пары в
пути, время линии «Closing time», маскировка ключа для логов. Сетевого запроса нет — его делает
программа (или следующий слой ядра), здесь ничего не ходит в сеть и не знает про логгер.

Грабли, проверенные вживую 25-09-2026 — и потому зашитые сюда, а не оставленные вызывающему:
  * Пара со СЛЭШЕМ ломает страницу: `EUR%2FUSD-OTC` сервер редиректит на `EUR/USD-OTC`, роутер
    видит два сегмента → 422 PAIR_REQUIRED. Это пример из их же документа. Работает только форма
    без слэша: `eurusd-otc`, `EURUSD`. Поэтому `build_url` принимает лишь такое имя, а
    `pair_slug` делает его из нашего `EUR/USD` / `EUR/USD OTC`.
  * `tz` с минутами (`-5:30`) binodex МОЛЧА игнорирует и рисует UTC. Линия закрытия, посчитанная
    нами в -5:30, разъехалась бы с часами кадра — отвергаем такой пояс сразу.
  * Умолчание `expiry` у binodex — «через 60 с от кадра», а кадр рендерится 2–5 с после запроса.
    Для опциона нужно абсолютное время конца: `expiry='option'` в профиле → `ЧЧ:ММ:СС` в поясе
    кадра, считает `build_url` из переданного `expiry_at`.
  * Неизвестный параметр binodex выбрасывает молча. Это же позволяет обойти их кэш «одинаковые
    ссылки 10 с отдают один кадр» служебным `n=<nonce>` — ссылка становится уникальной.

Ключ — секрет, и логи семьи уходят в Telegram-темы: в лог ссылку писать ТОЛЬКО через `redact`.
"""
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ORIGIN = 'https://binodex.app'

MODE_API = 'api'        # кадр по ссылке /og?key=, без браузера
MODE_TRADE = 'trade'    # кадр своим браузером на /trade — прежний путь
MODES = (MODE_API, MODE_TRADE)

EXPIRY_OPTION = 'option'   # особое значение профиля: время конца опциона подставляет программа

# Поле профиля → имя параметра в ссылке, В ПОРЯДКЕ ссылки. Порядок стабилен нарочно: одинаковый
# профиль даёт побайтно одинаковую ссылку (для их кэша и для сравнения по логам).
_PARAMS = (
    ('tf', 'tf'),
    ('chart_type', 'type'),        # колонка названа chart_type: `type` — служебное слово
    ('range', 'range'),
    ('indicators', 'indicators'),
    ('up_color', 'up'),
    ('down_color', 'down'),
    ('fill', 'fill'),
    ('expiry', 'expiry'),
    ('timer', 'timer'),
    ('tz', 'tz'),
    ('lang', 'lang'),
    ('theme', 'theme'),
    ('bg', 'bg'),
    ('veil', 'veil'),
    ('badges', 'badges'),
    ('labels', 'labels'),
    ('account', 'account'),
    ('payout', 'payout'),
    ('scale', 'scale'),
    ('inset', 'inset'),
    ('width', 'width'),
    ('height', 'height'),
    ('dpr', 'dpr'),
    ('format', 'format'),
    ('quality', 'quality'),
    ('timeout', 'timeout'),
)

# Имя пары в пути: латиница, цифры и то, что встречается у binodex в символах (`I:SPX-OTC`,
# `1000PEPE`). Слэша нет намеренно — см. грабли в шапке модуля.
_SLUG_RE = re.compile(r'[A-Za-z0-9:._-]+')
_OTC_SUFFIX_RE = re.compile(r'[\s-]+OTC$', re.I)
_OFFSET_RE = re.compile(r'(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?', re.I)
_KEY_RE = re.compile(r'([?&]key=)[^&#]*')


@dataclass(frozen=True)
class SnapshotProfile:
    """Профиль кадра — зеркало строки `settings.snapshot_profile`.

    None в поле параметра = параметр не передаётся, у binodex своё умолчание (оно не всегда
    годится: `tf` 5s, `expiry` 60, `dpr` 2, `tz` UTC — поэтому в наших профилях они заданы явно).
    Значения здесь НЕ проверяются на допустимость по списку binodex: CHECK-ограничения живут в
    таблице, а новые таймфреймы/индикаторы binodex не должны упираться в код ядра.
    """
    name: str
    mode: str = MODE_TRADE
    api_key_name: str | None = 'binodex_snapshot_key'
    tf: str | None = None
    chart_type: str | None = None
    range: str | None = None
    indicators: str | None = None
    up_color: str | None = None
    down_color: str | None = None
    fill: bool | None = None
    expiry: str | None = None
    timer: bool | None = None
    tz: str | None = None
    lang: str | None = None
    theme: str | None = None
    bg: str | None = None
    veil: int | None = None
    badges: bool | None = None
    labels: bool | None = None
    account: str | None = None
    payout: int | None = None
    scale: float | Decimal | None = None
    inset: int | None = None
    width: int | None = None
    height: int | None = None
    dpr: int | None = None
    format: str | None = None
    quality: int | None = None
    timeout: int | None = None

    @classmethod
    def from_row(cls, row) -> 'SnapshotProfile':
        """Профиль из строки БД (asyncpg.Record или dict). Лишние колонки таблицы (id_profile,
        description, updated_at) отбрасываются; недостающие получают умолчания класса."""
        data = dict(row)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_api(self) -> bool:
        """Профиль включён: кадр берётся по ссылке, а не своим браузером."""
        return self.mode == MODE_API

    def validate(self) -> None:
        """Проверка на СТАРТЕ программы — чтобы кривой профиль ронял подъём с понятным текстом,
        а не первый опцион посреди эфира.

        :raises ValueError: неизвестный mode; пояс, который binodex не поймёт или проигнорирует
        """
        if self.mode not in MODES:
            raise ValueError(f"Профиль кадра '{self.name}': неизвестный mode={self.mode!r}, "
                             f"ожидается один из {MODES}")
        tz_of(self.tz)


def tz_of(tz: str | None) -> tzinfo:
    """Пояс кадра из значения параметра `tz` — тем же правилом, каким его читает binodex.

    Проверено 25-09-2026: `+3`, `UTC+3` и `Europe/Moscow` дают UTC+3; пусто — UTC; `-5:30` binodex
    ИГНОРИРУЕТ (рисует UTC). Смещение с минутами поэтому отвергается: иначе мы посчитали бы линию
    закрытия в -5:30, а кадр нарисовал бы часы в UTC.

    :raises ValueError: смещение с минутами или неизвестное имя пояса
    """
    if tz is None or not tz.strip():
        return timezone.utc
    s = tz.strip()
    if s.upper() in ('UTC', 'GMT', 'Z'):
        return timezone.utc
    m = _OFFSET_RE.fullmatch(s)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if minutes:
            raise ValueError(f"Пояс кадра tz={tz!r}: смещение с минутами binodex игнорирует и рисует "
                             f"UTC (проверено 25-09-2026) — задайте целые часы или имя пояса")
        if hours > 14:
            raise ValueError(f"Пояс кадра tz={tz!r}: смещение больше 14 часов")
        delta = timedelta(hours=hours)
        return timezone(-delta if sign == '-' else delta)
    try:
        return ZoneInfo(s)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Пояс кадра tz={tz!r}: неизвестное имя пояса") from e


def pair_slug(name: str, otc: bool | None = None) -> str:
    """Имя пары для пути ссылки: без слэша и пробелов, в нижнем регистре.

        'EUR/USD', otc=True   → 'eurusd-otc'
        'EUR/USD OTC'         → 'eurusd-otc'   (так otc_app переименовывает пару для кадра)
        'EUR/USD-OTC'         → 'eurusd-otc'   (так пару зовёт сам binodex)
        'EUR/USD'             → 'eurusd'       (реальная пара — FIN)
        'TSLA'                → 'tsla'         (Future-акция)

    Суффикс OTC в имени сам ставит otc=True; явный otc=False при таком суффиксе — противоречие.

    ⚠️ Без суффикса binodex резолвит имя сам: `btc` → `BTC`, если такая пара торгуется, иначе
    `BTC-OTC`. Для OTC-потока otc=True передавать ОБЯЗАТЕЛЬНО, иначе у пары, которая есть в обоих
    видах, придёт кадр не того рынка.

    :raises ValueError: пустое имя, противоречие по OTC, недопустимые символы
    """
    s = (name or '').strip()
    m = _OTC_SUFFIX_RE.search(s)
    if m:
        if otc is False:
            raise ValueError(f"Пара {name!r}: в имени суффикс OTC, а передано otc=False")
        s, otc = s[:m.start()], True
    base = re.sub(r'[\s/]', '', s).lower()
    if not base:
        raise ValueError(f'Пустое имя пары: {name!r}')
    slug = base + ('-otc' if otc else '')
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"Пара {name!r}: недопустимые символы для пути ссылки ({slug!r})")
    return slug


def expiry_text(expiry_at: datetime, tz: str | None) -> str:
    """Время линии «Closing time» для параметра expiry: `ЧЧ:ММ:СС` в поясе кадра.

    Абсолютное время, а не «через N секунд»: кадр рендерится 2–5 с после запроса (замер 25-09),
    и относительная линия уехала бы на это время.

    :param expiry_at: конец опциона, ОБЯЗАТЕЛЬНО с поясом — наивное время тут означало бы гадание,
                      в каком поясе его считали
    :raises ValueError: наивное время или непригодный пояс
    """
    if expiry_at.tzinfo is None or expiry_at.utcoffset() is None:
        raise ValueError('expiry_at без пояса: передайте aware datetime (например, в UTC)')
    return expiry_at.astimezone(tz_of(tz)).strftime('%H:%M:%S')


def _fmt(value) -> str:
    """Значение параметра → текст ссылки: bool → 1/0, дробь без хвостовых нулей."""
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (float, Decimal)):
        return format(Decimal(str(value)).normalize(), 'f')
    return str(value)


def build_url(profile: SnapshotProfile, pair: str, key: str, *,
              expiry_at: datetime | None = None, nonce: str | int | None = None,
              origin: str = ORIGIN) -> str:
    """Ссылка на кадр: `<origin>/og/<пара>?key=…&<параметры профиля>[&n=<nonce>]`.

    :param profile: профиль кадра (строка settings.snapshot_profile)
    :param pair: имя пары БЕЗ слэша — результат `pair_slug`. Слэш не чинится здесь молча: его
                 появление значит, что вызывающий обошёл `pair_slug`, и это надо увидеть.
    :param key: API-ключ партнёра. В логи — только через `redact`.
    :param expiry_at: конец опциона (aware datetime); обязателен, если в профиле expiry='option'
    :param nonce: служебный `n=` — делает ссылку уникальной в обход их кэша «10 с — один кадр».
                  Нужен, когда два кадра одной пары могут прийтись на одни 10 секунд (вход и
                  быстрый итог, два инстанса одной пары).
    :param origin: адрес платформы; менять только для стенда
    :raises ValueError: нет ключа, пара со слэшем/мусором, expiry='option' без expiry_at,
                        непригодный пояс
    """
    if not key or not key.strip():
        raise ValueError('Пустой API-ключ snapshot')
    if not pair or not _SLUG_RE.fullmatch(pair):
        raise ValueError(f"Пара {pair!r} не годится для ссылки: нужна форма без слэша "
                         f"(eurusd-otc, EURUSD) — используйте pair_slug")
    parts = [f'key={quote(key.strip(), safe="")}']
    for field_name, param in _PARAMS:
        value = getattr(profile, field_name)
        if value is None:
            continue
        if field_name == 'expiry' and value == EXPIRY_OPTION:
            if expiry_at is None:
                raise ValueError(f"Профиль кадра '{profile.name}': expiry='option', "
                                 f"но время конца опциона (expiry_at) не передано")
            value = expiry_text(expiry_at, profile.tz)
        elif field_name == 'tz':
            tz_of(value)                          # отвергнуть -5:30 ДО запроса, а не после
        elif field_name in ('up_color', 'down_color'):
            value = str(value).lstrip('#')        # в таблице без '#', но '#' в ссылке — якорь
        parts.append(f'{param}={quote(_fmt(value), safe=",:")}')
    if nonce is not None:
        parts.append(f'n={quote(str(nonce), safe="")}')
    return f'{origin.rstrip("/")}/og/{quote(pair, safe=":-.")}?' + '&'.join(parts)


def redact(url: str) -> str:
    """Ссылка для лога: значение `key=` заменено на `<KEY>`. Логи семьи уходят в Telegram-темы."""
    return _KEY_RE.sub(r'\1<KEY>', url)
