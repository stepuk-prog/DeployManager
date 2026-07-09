"""IMAP-чтение кода Privy (Gmail) — синхронные функции, зовутся из async через
asyncio.to_thread (imaplib блокирующий). Порт из BinoOptions apps/binodex_session.py.

mail_app_pass — 16-символьный Gmail app-password (обычный пароль для IMAP не годится,
нужна включённая 2FA). Код приходит с двух адресов (no-reply@privy.io и
no-reply@mail.privy.io) → фильтр по домену-подстроке PRIVY_FROM матчит оба.
"""
import email
import imaplib
import re
import time
from email.header import decode_header, make_header

from tools.cookies.logs import init_logger
from tools.cookies.settings.constant import (PRIVY_CODE_POLL_EVERY, PRIVY_CODE_WAIT_SECONDS,
                               PRIVY_FROM, PRIVY_SUBJECT_HINT)

logger = init_logger(__name__)


def imap_connect(mail: str, app_pass: str) -> imaplib.IMAP4_SSL:
    # Gmail показывает app-пароль группами по 4 через пробел ("abcd efgh ijkl mnop"); в БД он мог
    # сохраниться с пробелами → login их не жуёт. Чистим пробелы (и по краям) перед входом.
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
    conn.login((mail or "").strip(), (app_pass or "").replace(" ", ""))
    conn.select("INBOX")
    return conn


def privy_uids(conn: imaplib.IMAP4_SSL) -> list[int]:
    # noinspection PyTypeChecker
    typ, data = conn.uid("search", None, f'(FROM "{PRIVY_FROM}")')  # None — charset
    return [int(x) for x in data[0].split()] if data and data[0] else []


def _extract_code(conn: imaplib.IMAP4_SSL, uid: int) -> str | None:
    typ, md = conn.uid("fetch", str(uid), "(RFC822)")
    if not md or not md[0]:
        return None
    msg = email.message_from_bytes(md[0][1])
    subject = str(make_header(decode_header(msg.get("Subject", "")))).lower()
    if PRIVY_SUBJECT_HINT not in subject:
        return None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() in ("text/plain", "text/html"):
            body = part.get_payload(decode=True)
            if not body:
                continue
            try:
                txt = body.decode(part.get_content_charset() or "utf-8", "ignore")
            except (Exception,) as error:
                logger.warning("decode письма Privy: %s", error)
                continue
            match = re.search(r"\b(\d{6})\b", txt)
            if match:
                return match.group(1)
    return None


def wait_for_code(conn: imaplib.IMAP4_SSL, baseline: set[int]) -> str:
    """Первое письмо с кодом ПОСЛЕ запроса (uid не из baseline) — старые коды (живут 10 мин,
    их в ящике несколько) игнорируем. Таймаут — RuntimeError."""
    deadline = time.monotonic() + PRIVY_CODE_WAIT_SECONDS
    while time.monotonic() < deadline:
        conn.noop()
        for uid in sorted(set(privy_uids(conn)) - baseline, reverse=True):
            code = _extract_code(conn, uid)
            if code:
                return code
        time.sleep(PRIVY_CODE_POLL_EVERY)
    raise RuntimeError(f"код Privy не пришёл за {PRIVY_CODE_WAIT_SECONDS}с")


def purge_privy(conn: imaplib.IMAP4_SSL) -> int:
    """Удалить все письма Privy (одноразовые коды). Gmail: ярлык \\Trash + \\Deleted + expunge.
    Возвращает число удалённых."""
    uids = privy_uids(conn)
    if not uids:
        return 0
    uid_set = ",".join(str(u) for u in uids)
    for store in (("+X-GM-LABELS", "\\Trash"), ("+FLAGS", "\\Deleted")):
        try:
            conn.uid("STORE", uid_set, *store)
        except (Exception,):
            pass
    try:
        conn.expunge()
    except (Exception,):
        pass
    return len(uids)


def logout(conn: imaplib.IMAP4_SSL) -> None:
    try:
        conn.logout()
    except (Exception,):
        pass
