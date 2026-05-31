"""Единая точка пользовательского ввода — поддержка неинтерактивного режима (--yes/CLI).

В неинтерактивном режиме ask() возвращает безопасный default (без блокировки на input),
а confirm() — True только при --yes, иначе False (т.е. по умолчанию НЕ выполнять).
"""
import sys

INTERACTIVE = True
ASSUME_YES = False


def set_mode(interactive: bool, assume_yes: bool = False) -> None:
    global INTERACTIVE, ASSUME_YES
    INTERACTIVE = interactive
    ASSUME_YES = assume_yes


def ask(prompt: str, default: str = "") -> str:
    """Запросить строку. В неинтерактиве — вернуть default без ввода."""
    if not INTERACTIVE:
        return default
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def confirm(prompt: str) -> bool:
    """Да/нет. --yes → True; неинтерактив без --yes → False (безопасный отказ)."""
    if ASSUME_YES:
        return True
    if not INTERACTIVE:
        return False
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def _checkbox_text(title: str, labels: list[str]) -> list[int]:
    """Откат: текстовый ввод номеров (когда чек-боксы недоступны)."""
    print(title)
    for i, lab in enumerate(labels, 1):
        print(f"  [{i}] {lab}")
    raw = input("Номера через запятую / 'all': ").strip()
    if raw.lower() == "all":
        return list(range(len(labels)))
    return [int(t) - 1 for t in raw.replace(" ", "").split(",")
            if t.isdigit() and 1 <= int(t) <= len(labels)]


def checkbox(title: str, labels: list[str], default_all: bool = False) -> list[int]:
    """Множественный выбор чек-боксами → список выбранных индексов (0-based).

    Неинтерактив: все (default_all/--yes) или пусто. Интерактив + настоящий TTY:
    questionary-чекбоксы (пробел/enter). Без TTY (напр. Run-консоль IDE) или без
    questionary — откат на текстовый ввод номеров (с пояснением причины).
    """
    if not labels:
        return []
    if not INTERACTIVE:
        return list(range(len(labels))) if (ASSUME_YES or default_all) else []
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("(терминал без TTY — чек-боксы недоступны; включи «Emulate terminal in "
              "output console» в IDE или запусти в обычном терминале)")
        return _checkbox_text(title, labels)
    try:
        import questionary
        choices = [questionary.Choice(title=lab, value=i, checked=default_all)
                   for i, lab in enumerate(labels)]
        picked = questionary.checkbox(title, choices=choices).ask()
        return picked if picked is not None else []
    except Exception as e:
        print(f"(чек-боксы недоступны: {e} — текстовый ввод)")
        return _checkbox_text(title, labels)
