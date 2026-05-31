"""Единая точка пользовательского ввода — поддержка неинтерактивного режима (--yes/CLI).

В неинтерактивном режиме ask() возвращает безопасный default (без блокировки на input),
а confirm() — True только при --yes, иначе False (т.е. по умолчанию НЕ выполнять).
"""
import sys

INTERACTIVE = True
ASSUME_YES = False
# Бэкенд интерактива (GUI). Если задан — ask/confirm/checkbox рисует он (диалоги),
# CLI-реализация (questionary/input) не используется. Ядро остаётся UI-агностичным.
_BACKEND = None


def set_mode(interactive: bool, assume_yes: bool = False) -> None:
    global INTERACTIVE, ASSUME_YES
    INTERACTIVE = interactive
    ASSUME_YES = assume_yes


def set_backend(backend) -> None:
    """Подключить GUI-бэкенд с async-методами ask(prompt,default)/confirm(prompt)/
    checkbox(title,labels,default_all). None — вернуть CLI-поведение."""
    global _BACKEND
    _BACKEND = backend


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _input(prompt: str) -> str:
    """input() с защитой от EOF/битой кодировки (не валим программу)."""
    try:
        return input(prompt).strip()
    except (EOFError, UnicodeDecodeError):
        return ""


async def ask(prompt: str, default: str = "") -> str:
    """Запросить строку. GUI-бэкенд → диалог; иначе questionary (TTY) / input / default."""
    if _BACKEND is not None:
        return await _BACKEND.ask(prompt, default)
    if not INTERACTIVE:
        return default
    if _has_tty():
        try:
            import questionary
            ans = await questionary.text(prompt, default=default).ask_async()
            return (ans if ans is not None else default).strip() or default
        except Exception:
            pass
    return _input(f"{prompt}{(' [' + default + ']') if default else ''}: ") or default


async def confirm(prompt: str) -> bool:
    """Да/нет. GUI-бэкенд → диалог; иначе --yes/неинтерактив/questionary/input."""
    if _BACKEND is not None:
        return bool(await _BACKEND.confirm(prompt))
    if ASSUME_YES:
        return True
    if not INTERACTIVE:
        return False
    if _has_tty():
        try:
            import questionary
            return bool(await questionary.confirm(prompt, default=False).ask_async())
        except Exception:
            pass
    return _input(f"{prompt} [y/N]: ").lower() == "y"


async def select(title: str, labels: list[str], default_index: int = 0) -> int | None:
    """Одиночный выбор из списка → индекс (0-based) или None (отмена).
    GUI-бэкенд → диалог; TTY → questionary.select; без TTY → ввод номера."""
    if not labels:
        return None
    if _BACKEND is not None:
        return await _BACKEND.select(title, labels, default_index)
    if not INTERACTIVE:
        return default_index
    if _has_tty():
        try:
            import questionary
            choices = [questionary.Choice(title=lab, value=i) for i, lab in enumerate(labels)]
            r = await questionary.select(title, choices=choices,
                                         default=choices[default_index]).ask_async()
            return r if r is not None else None
        except Exception:
            pass
    print(title)
    for i, lab in enumerate(labels, 1):
        print(f"  [{i}] {lab}")
    raw = _input(f"Номер [{default_index + 1}]: ")
    if not raw:
        return default_index
    return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(labels) else None


def _checkbox_text(title: str, labels: list[str]) -> list[int]:
    """Откат: текстовый ввод номеров (когда чек-боксы недоступны)."""
    print(title)
    for i, lab in enumerate(labels, 1):
        print(f"  [{i}] {lab}")
    raw = _input("Номера через запятую / 'all': ")
    if raw.lower() == "all":
        return list(range(len(labels)))
    return [int(t) - 1 for t in raw.replace(" ", "").split(",")
            if t.isdigit() and 1 <= int(t) <= len(labels)]


async def checkbox(title: str, labels: list[str], default_all: bool = False) -> list[int]:
    """Множественный выбор чек-боксами → список выбранных индексов (0-based). Корутина:
    вся программа крутится в asyncio.run, поэтому используем ask_async() (sync .ask()
    внутри работающего loop падает «run_async was never awaited»).

    Неинтерактив: все (default_all/--yes) или пусто. Интерактив + настоящий TTY:
    questionary-чекбоксы (пробел/enter). Без TTY (Run-консоль IDE) или без questionary —
    откат на текстовый ввод номеров (с пояснением причины).
    """
    if not labels:
        return []
    if _BACKEND is not None:
        return await _BACKEND.checkbox(title, labels, default_all)
    if not INTERACTIVE:
        return list(range(len(labels))) if (ASSUME_YES or default_all) else []
    if not _has_tty():
        print("(терминал без TTY — чек-боксы недоступны; включи «Emulate terminal in "
              "output console» в IDE или запусти в обычном терминале)")
        return _checkbox_text(title, labels)
    try:
        import questionary
        choices = [questionary.Choice(title=lab, value=i, checked=default_all)
                   for i, lab in enumerate(labels)]
        picked = await questionary.checkbox(title, choices=choices).ask_async()
        return picked if picked is not None else []
    except Exception as e:
        print(f"(чек-боксы недоступны: {e} — текстовый ввод)")
        return _checkbox_text(title, labels)
