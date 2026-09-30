#!/usr/bin/env bash
# Installs the tray-icon library, an `agent-tray` launcher, a menu entry and login autostart.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/agent_tray.py"

if ! python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null; then
  sudo apt install -y gir1.2-ayatanaappindicator3-0.1
fi

# Stop any running copy (old claude-tray or agent-tray) and remove the old claude-tray install.
pkill -f "^(/usr/bin/)?python3 .*(agent|claude)[-_]tray" 2>/dev/null || true
rm -f ~/.local/bin/claude-tray ~/.config/autostart/claude-tray.desktop ~/.local/share/applications/claude-tray.desktop
rm -rf ~/.cache/claude-tray

chmod +x "$APP"
mkdir -p ~/.local/bin ~/.config/autostart ~/.local/share/applications
ln -sf "$APP" ~/.local/bin/agent-tray

DESKTOP="[Desktop Entry]
Type=Application
Name=Agent Tray
Comment=Claude Code and ChatGPT sessions in the top bar
Exec=/usr/bin/python3 $APP
Icon=$HOME/.cache/agent-tray/agent-tray-idle-0.svg
Terminal=false
X-GNOME-Autostart-enabled=true"
echo "$DESKTOP" > ~/.config/autostart/agent-tray.desktop
echo "$DESKTOP" > ~/.local/share/applications/agent-tray.desktop

sleep 1
python3 "$APP"
echo "Agent Tray is running - look for the icon in the top bar. It will also start on login."
