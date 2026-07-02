"""Единая точка пользовательского ввода — поддержка неинтерактивного режима (--yes/CLI).

В неинтерактивном режиме ask() возвращает безопасный default (без блокировки на input),
а confirm() — True только при --yes, иначе False (т.е. по умолчанию НЕ выполнять).
"""
import sys
from typing import Any

INTERACTIVE = True
ASSUME_YES = False
# Бэкенд интерактива (GUI). Если задан — ask/confirm/checkbox рисует он (диалоги),
# CLI-реализация (questionary/input) не используется. Ядро остаётся UI-агностичным.
# Тип Any: у бэкенда динамический набор async-методов (ask/confirm/select/combobox/checkbox),
# иначе статанализ (PyCharm) ругается на обращения к атрибутам None.
_BACKEND: Any = None


def set_mode(interactive: bool, assume_yes: bool = False) -> None:
    global INTERACTIVE, ASSUME_YES
    INTERACTIVE = interactive
    ASSUME_YES = assume_yes


def set_backend(backend: Any) -> None:
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


async def ask(prompt: str, default: str = "", cancelable: bool = False,
              ok_label: str | None = None, cancel_label: str | None = None) -> str | None:
    """Запросить строку. GUI-бэкенд → диалог; иначе questionary (TTY) / input / default.
    cancelable=True — у ввода появляется «Отмена» (→ None = отмена операции; иначе всегда str).
    ok_label/cancel_label — подписи кнопок в GUI (для мастеров «Продолжить»/«Отмена»)."""
    if _BACKEND is not None:
        kw: dict[str, Any] = {"cancelable": cancelable} if cancelable else {}
        if ok_label is not None:
            kw["ok_label"] = ok_label
        if cancel_label is not None:
            kw["cancel_label"] = cancel_label
        return await _BACKEND.ask(prompt, default, **kw)
    if not INTERACTIVE:
        return default
    if _has_tty():
        try:
            import questionary
            ans = await questionary.text(prompt, default=default).ask_async()
            if ans is None:                      # Ctrl-C / отмена в questionary
                return None if cancelable else default
            return ans.strip() or default
        except (Exception,):
            pass
    return _input(f"{prompt}{(' [' + default + ']') if default else ''}: ") or default


def progress(text: str) -> None:
    """Краткий статус долгой операции (не диалог). GUI → строка статуса + спиннер;
    CLI → печать. Fire-and-forget, без await (вызывается из горячих циклов)."""
    if _BACKEND is not None and hasattr(_BACKEND, "progress"):
        _BACKEND.progress(text)
    elif INTERACTIVE and text:
        print(text)


async def radio(title: str, labels: list[str], default_index: int = 0) -> int | None:
    """Выбор ровно одного варианта радиокнопками → индекс (0-based) или None (отмена).
    GUI-бэкенд → RadioGroup; без GUI — поведение select()."""
    if not labels:
        return None
    if _BACKEND is not None and hasattr(_BACKEND, "radio"):
        return await _BACKEND.radio(title, labels, default_index)
    return await select(title, labels, default_index)


async def confirm(prompt: str, danger: bool = False,
                  ok_label: str | None = None, cancel_label: str | None = None) -> bool:
    """Да/нет. GUI-бэкенд → диалог; иначе --yes/неинтерактив/questionary/input.
    danger=True — деструктивная операция: в GUI «Да» красная + красный текст-предупреждение.
    ok_label/cancel_label — подписи кнопок в GUI (напр. «Продолжить»/«Отмена»)."""
    if _BACKEND is not None:
        kw: dict[str, Any] = {}
        if ok_label is not None:
            kw["ok_label"] = ok_label
        if cancel_label is not None:
            kw["cancel_label"] = cancel_label
        return bool(await _BACKEND.confirm(prompt, danger=danger, **kw))
    if ASSUME_YES:
        return True
    if not INTERACTIVE:
        return False
    if _has_tty():
        try:
            import questionary
            return bool(await questionary.confirm(prompt, default=False).ask_async())
        except (Exception,):
            pass
    return _input(f"{prompt} [y/N]: ").lower() == "y"


async def combobox(title: str, labels: list[str], default_index: int = 0) -> int | None:
    """Выбор одного варианта через выпадающий список (combobox). GUI → ft.Dropdown;
    без GUI — поведение как у select() (questionary/ввод номера). → индекс или None."""
    if not labels:
        return None
    if _BACKEND is not None and hasattr(_BACKEND, "combobox"):
        return await _BACKEND.combobox(title, labels, default_index)
    return await select(title, labels, default_index)


async def select(title: str, labels: list[str], default_index: int = 0,
                 details: "list[str] | None" = None,
                 colors: "list[str] | None" = None,
                 cancel_in_grid: bool = False) -> int | None:
    """Одиночный выбор из списка → индекс (0-based) или None (отмена).
    details (по одному на label) — описания (GUI → тултип). colors — цвет кнопки
    (green/blue/red/teal). cancel_in_grid — «Отмена» кнопкой в сетке. CLI игнорит
    colors/cancel_in_grid. GUI-бэкенд → диалог; TTY → questionary; без TTY → номер."""
    if not labels:
        return None
    if _BACKEND is not None:
        return await _BACKEND.select(title, labels, default_index, details,
                                     colors, cancel_in_grid)
    if not INTERACTIVE:
        return default_index
    if _has_tty():
        try:
            import questionary
            choices = [questionary.Choice(title=lab, value=i) for i, lab in enumerate(labels)]
            r = await questionary.select(title, choices=choices,
                                         default=choices[default_index]).ask_async()
            return r if r is not None else None
        except (Exception,):
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


async def checkbox(title: str, labels: list[str], default_all: bool = False,
                   default_checked: list[bool] | None = None, ok_label: str | None = None,
                   cancel_label: str | None = None, danger: bool = False,
                   dialog_title: str | None = None) -> list[int]:
    """Множественный выбор чек-боксами → список выбранных индексов (0-based). Корутина:
    вся программа крутится в asyncio.run, поэтому используем ask_async() (sync .ask()
    внутри работающего loop падает «run_async was never awaited»).

    default_checked — по-элементная предотметка (len == len(labels)); приоритетнее default_all
    (напр. предотметить ноды, где программа уже стоит). Неинтерактив: предотмеченные
    (или все при default_all/--yes), иначе пусто. Интерактив + настоящий TTY: questionary-чекбоксы
    (пробел/enter). Без TTY или без questionary — откат на текстовый ввод номеров.
    """
    if not labels:
        return []
    checked = (default_checked if default_checked and len(default_checked) == len(labels)
               else [default_all] * len(labels))
    if _BACKEND is not None:
        kw: dict[str, Any] = {"danger": danger} if danger else {}
        if ok_label is not None:
            kw["ok_label"] = ok_label
        if cancel_label is not None:
            kw["cancel_label"] = cancel_label
        if dialog_title is not None:
            kw["dialog_title"] = dialog_title
        return await _BACKEND.checkbox(title, labels, default_all, checked, **kw)
    if not INTERACTIVE:
        if ASSUME_YES:
            return list(range(len(labels)))
        return [i for i, c in enumerate(checked) if c]
    if not _has_tty():
        print("(терминал без TTY — чек-боксы недоступны; включи «Emulate terminal in "
              "output console» в IDE или запусти в обычном терминале)")
        return _checkbox_text(title, labels)
    try:
        import questionary
        choices = [questionary.Choice(title=lab, value=i, checked=checked[i])
                   for i, lab in enumerate(labels)]
        picked = await questionary.checkbox(title, choices=choices).ask_async()
        return picked if picked is not None else []
    except Exception as e:
        print(f"(чек-боксы недоступны: {e} — текстовый ввод)")
        return _checkbox_text(title, labels)
