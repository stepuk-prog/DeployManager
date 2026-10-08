#!/bin/bash
# Ярлык DeployManager для меню KDE/GNOME (и опц. рабочего стола) — пути берутся из папки,
# где лежит этот клон, поэтому после `git clone` в любое место достаточно запустить:
#   bin/install-desktop.sh            # меню приложений
#   bin/install-desktop.sh --desktop  # + ярлык на рабочем столе
#
# StartupWMClass=flet — app_id окна Flet-клиента на Wayland. Без него Plasma не
# связывает окно с закреплённым значком и показывает в панели отдельную кнопку с иконкой
# по умолчанию.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
NAME="DeployManager.desktop"

chmod +x "$DIR/bin/run.sh"
mkdir -p "$APPS"
cat > "$APPS/$NAME" <<EOF
[Desktop Entry]
Type=Application
Name=Deploy Manager
Comment=Деплой и управление программами на нодах (Flet GUI)
Exec=$DIR/bin/run.sh
Path=$DIR
Icon=$DIR/icon.png
Terminal=false
Categories=Development;
StartupWMClass=flet
EOF
chmod +x "$APPS/$NAME"
echo "✅ меню: $APPS/$NAME"

if [[ "${1:-}" == "--desktop" ]]; then
    DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
    mkdir -p "$DESK"
    cp -f "$APPS/$NAME" "$DESK/$NAME"
    chmod +x "$DESK/$NAME"
    echo "✅ рабочий стол: $DESK/$NAME"
fi

# Обновить кэш меню (KDE — kbuildsycoca; бывает долгим — в фоне, с потолком).
if command -v kbuildsycoca6 >/dev/null; then
    (timeout 60 kbuildsycoca6 >/dev/null 2>&1 &)
elif command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
echo "Если значок уже закреплён в панели — открепи и закрепи заново."
