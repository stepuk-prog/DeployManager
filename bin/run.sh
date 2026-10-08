#!/bin/bash
# Запуск GUI DeployManager. Пути — от расположения скрипта (работает из любого клона),
# ссылку-ярлык на рабочем столе тоже понимает (readlink -f).
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$DIR" || exit 1
source "$DIR/.venv/bin/activate"
exec python "$DIR/gui_main.py"
