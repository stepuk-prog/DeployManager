"""Единая точка пользовательского ввода — поддержка неинтерактивного режима (--yes/CLI).

В неинтерактивном режиме ask() возвращает безопасный default (без блокировки на input),
а confirm() — True только при --yes, иначе False (т.е. по умолчанию НЕ выполнять).
"""
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
