"""Вкладка «Cookies»: вход на binodex идёт ЯДРОМ и знает оба механизма авторизации.

Тест сторожит ровно ту поломку, из-за которой инструмент переводили на ядро 21-09-2026: своя
копия логина знала только Privy (письмо от privy.io, код в ТЕЛЕ, признак входа privy:token), и
на аккаунте с собственной авторизацией binodex «не видела почту» — письмо от mail.binodex.io с
кодом в ТЕМЕ проходило мимо фильтра, флоу досиживал 120с и падал по таймауту.

Без сети и БД: IMAP подменён заглушкой, письма собраны из email.message.
"""
from email.message import EmailMessage

from binocore import binodex as core
from tools.cookies.apps.binodex import missing_login_selectors

# Как строки лежат в settings.binodex_settings (снято с боевой БД 21-09-2026).
DB_SEL = {
    "login_open": "#login_open",
    "login_email": "#email-input",
    "login_submit": 'button:has-text("Submit")',
    "login_code_inputs": 'input[inputmode="numeric"]',
    "login_mail_from": "privy.io,binodex.io",
    "login_mail_subject": "code",
    "session_keys": "privy:token,ownAuthSession",
}


class _FakeImap:
    """Заглушка IMAP: отдаёт письма по uid и запоминает поисковые запросы."""

    def __init__(self, mails: dict):
        self._mails = mails                      # uid → (отправитель, EmailMessage)
        self.searched: list[str] = []

    def uid(self, command, *args):
        if command == "search":
            query = args[1]
            self.searched.append(query)
            sender = query.split('"')[1]
            hits = [str(uid) for uid, (frm, _) in self._mails.items() if sender in frm]
            return "OK", [" ".join(hits).encode()]
        if command == "fetch":
            _, msg = self._mails[int(args[0])]
            return "OK", [(b"", msg.as_bytes())]
        raise AssertionError(f"неожиданная команда IMAP: {command}")


def _privy_letter(code: str) -> EmailMessage:
    """Письмо Privy: тема без кода, код в теле."""
    msg = EmailMessage()
    msg["Subject"] = "Your login code for BinoDex"
    msg["From"] = "no-reply@mail.privy.io"
    msg.set_content(f"Enter this code to continue: {code}")
    return msg


def _own_auth_letter(code: str) -> EmailMessage:
    """Письмо собственной авторизации binodex: код стоит прямо в ТЕМЕ."""
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your binodex code"
    msg["From"] = "account@mail.binodex.io"
    msg.set_content("Hello")
    return msg


def test_selectors_required_list_is_core_one():
    """Инструмент требует ровно тот набор селекторов, что и программы флота."""
    assert missing_login_selectors({}) == list(core.REQUIRED_SELECTORS)
    assert missing_login_selectors({k: "x" for k in core.REQUIRED_SELECTORS}) == []


def test_settings_read_from_db_row():
    assert core.mail_froms(DB_SEL) == ("privy.io", "binodex.io")
    assert core.subject_hint(DB_SEL) == "code"
    assert core.session_keys(DB_SEL) == ("privy:token", "ownAuthSession")


def test_search_covers_both_senders():
    """Поиск идёт по ОБОИМ доменам — прежний фильтр спрашивал только privy.io."""
    imap = _FakeImap({1: ("no-reply@mail.privy.io", _privy_letter("111111")),
                      2: ("account@mail.binodex.io", _own_auth_letter("222222"))})
    assert core.code_uids(imap, core.mail_froms(DB_SEL)) == [1, 2]
    assert any("binodex.io" in q for q in imap.searched)


def test_code_from_subject_and_from_body():
    """Код читается и из темы (binodex), и из тела (Privy)."""
    imap = _FakeImap({1: ("no-reply@mail.privy.io", _privy_letter("111111")),
                      2: ("account@mail.binodex.io", _own_auth_letter("222222"))})
    hint = core.subject_hint(DB_SEL)
    assert core.extract_code(imap, 1, hint) == "111111"
    assert core.extract_code(imap, 2, hint) == "222222"


def test_wait_for_code_takes_fresh_letter_only():
    """Старые письма (baseline) игнорируются — берём код из пришедшего ПОСЛЕ запроса."""
    imap = _FakeImap({1: ("no-reply@mail.privy.io", _privy_letter("111111")),
                      2: ("account@mail.binodex.io", _own_auth_letter("222222"))})
    imap.noop = lambda: None
    code, conn = core.wait_for_code(imap, baseline={1}, froms=core.mail_froms(DB_SEL),
                                    hint=core.subject_hint(DB_SEL))
    assert (code, conn) == ("222222", imap)   # соединение возвращается: им чистятся письма
