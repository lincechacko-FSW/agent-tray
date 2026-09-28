#!/usr/bin/env bash
# Installs the tray-icon library, a `claude-tray` launcher, a menu entry and login autostart.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/claude_tray.py"

if ! python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null; then
  sudo apt install -y gir1.2-ayatanaappindicator3-0.1
fi
chmod +x "$APP"
mkdir -p ~/.local/bin ~/.config/autostart ~/.local/share/applications
ln -sf "$APP" ~/.local/bin/claude-tray

DESKTOP="[Desktop Entry]
Type=Application
Name=Claude Tray
Comment=Claude Code sessions in the top bar
Exec=/usr/bin/python3 $APP
Icon=$HOME/.cache/claude-tray/claude-tray-idle.svg
Terminal=false
X-GNOME-Autostart-enabled=true"
echo "$DESKTOP" > ~/.config/autostart/claude-tray.desktop
echo "$DESKTOP" > ~/.local/share/applications/claude-tray.desktop

python3 "$APP"
echo "Claude Tray is running - look for the icon in the top bar. It will also start on login."
