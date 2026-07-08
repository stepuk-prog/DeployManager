"""Точка входа DeployManager (интерактивный и неинтерактивный режимы)."""
import argparse
import asyncio

from cli import run


def _parse_args():
    p = argparse.ArgumentParser(description="DeployManager — деплой проектов на ноды")
    p.add_argument("--project", help="папка проекта (по умолчанию спросит/cwd)")
    p.add_argument("--action",
                   choices=["new", "add", "check", "create", "state", "manage", "uninstall",
                            "sync", "infra", "sessions", "cookies", "setup-node"],
                   help="ветка без меню: new (с нуля) / add (добавить сервер) / "
                        "check (версии) / create / state / manage / uninstall / sync (.env+юниты) / "
                        "infra (деплой control-plane компонента: --component) / "
                        "sessions (юзерботы: логин → session_string) / cookies (GUI-only)")
    p.add_argument("--command", choices=["start", "stop", "restart"],
                   help="для --action manage: команда сервису через watchdog")
    p.add_argument("--component", choices=["GD", "WD", "CD", "DispatcherCtl"],
                   help="для --action infra: какой control-plane компонент деплоить")
    p.add_argument("--check", action="store_true",
                   help="для --action infra: только сверка версий на нодах (VERSION vs git), без деплоя")
    p.add_argument("--nodes", help="ноды: 'all' или список имён/ip/номеров через запятую")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="предпросмотр без изменений")
    p.add_argument("--yes", action="store_true", help="неинтерактивно: не спрашивать, безопасные дефолты")
    return p.parse_args()


def main():
    args = _parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nПрервано.")


if __name__ == "__main__":
    main()
