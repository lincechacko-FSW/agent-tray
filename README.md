# agent-tray

A Linux top-bar indicator for AI coding agents. See your running sessions, the model each one uses, how much context is left, token usage and time spent, and resume any session in one click without typing in a terminal.

Currently supports **Claude Code**. Support for other agents is planned.

> Unofficial community tool. Not affiliated with or endorsed by Anthropic.

## Features

- **Icon that stays in the top bar.** It shows how many sessions are running. Grey means no sessions, orange means all idle, green means at least one is busy.
- **Session list on click.** Each running session is listed with status, model, context left and time running.
- **Dashboard.** One card per session with:
  - status (busy / idle / ended) and project folder
  - model(s) used, including subagent models, and whether it is a 1M-context model
  - context window used and left, as a bar
  - tokens: input, output, cache read, cache write (subagents included)
  - time running, active time, time since last message, API time and cost when available
- **Resume in one click.** Ended sessions open in a new terminal with `claude --resume`. Running sessions can be opened as a forked copy, which leaves the original untouched.
- **Today's total.** Token usage across all sessions today.
- **Light on resources.** It reads only the new lines in session files, updates the moment a session starts or stops, and redraws only when something changed. It has no pip dependencies.

## Requirements

- Linux with a desktop that supports AppIndicator / StatusNotifier tray icons. Tested on Ubuntu 24.04 (GNOME, Wayland).
  - GNOME needs the **AppIndicator** extension. Ubuntu has it enabled by default (`ubuntu-appindicators@ubuntu.com`).
  - KDE, XFCE, Cinnamon and MATE support tray icons out of the box.
- Python 3.10+ with PyGObject (GTK 3). Preinstalled on Ubuntu.
- `gir1.2-ayatanaappindicator3-0.1`, which `install.sh` installs for you.
- `gnome-terminal`, used by the resume buttons.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed, with sessions in `~/.claude/`.

## Install

```bash
git clone https://github.com/lincechacko-FSW/agent-tray.git
cd agent-tray
bash install.sh
```

`install.sh` will:
1. Install the tray-icon library with `apt` (it asks for your sudo password)
2. Add a `claude-tray` command in `~/.local/bin`
3. Add an app-menu entry and start the app automatically at login
4. Start it now

## Usage

| Action | How |
|---|---|
| Start | `claude-tray`, or open **Claude Tray** from the app menu |
| See sessions | Click the icon in the top bar |
| Open full dashboard | Click the icon, then **Open dashboard…**, or middle-click the icon |
| Resume an ended session | **Open session** on its card, or icon menu, then **Open session ▸**, then the session |
| Open a running session | **Open copy** on its card (starts a forked copy in a new terminal) |
| Open project folder | **Open folder** on a card |
| Copy the resume command | **⋯** on a card |
| Refresh now | Icon menu, then **Refresh** |
| Stop | Icon menu, then **Quit** |

Closing the dashboard only hides it, and closing the terminal you started from doesn't stop the app. Starting it a second time won't add a second icon.

### Command-line options

```bash
claude-tray                # start in the background (default)
claude-tray --foreground   # stay attached to the terminal, logs to stdout
claude-tray --dump         # print the current session data as JSON and exit
```

When running in the background, logs go to `~/.cache/claude-tray/claude-tray.log`.

## What the numbers mean

- **Context left:** the size of the last request (input + cache + output) compared with the model's context window (200k, or 1M for `[1m]` models). This is how full the conversation is, not your plan quota.
- **Active time:** time between messages, not counting gaps longer than 5 minutes.
- **Today:** tokens from all sessions since local midnight. Cache reads are shown separately because they are much larger and cheaper.
- **Plan usage limits** (5-hour and weekly) are not stored on your machine, so they can't be shown. Run `/usage` inside Claude Code to see them.

## How it works

Claude Code writes everything the app needs to disk:

- `~/.claude/sessions/<pid>.json`: one file per open session, with name, folder, start time and busy/idle status. The app checks each PID is still alive to spot stale files.
- `~/.claude/projects/<project>/<session-id>.jsonl`: each session's history, with the model and token usage of every response. Subagent histories are in `<session-id>/subagents/`.

A background thread reads these files and watches the sessions folder for changes (inotify). For history files, it reads only the lines added since its last check. The GTK main thread receives a snapshot only when something changed.

## Uninstall

```bash
rm ~/.local/bin/claude-tray ~/.config/autostart/claude-tray.desktop ~/.local/share/applications/claude-tray.desktop
rm -rf ~/.cache/claude-tray
```

## Troubleshooting

- **No icon appears:** on GNOME, check the AppIndicator extension is enabled with `gnome-extensions list --enabled | grep -i appindicator`.
- **`Namespace AyatanaAppIndicator3 not available`:** run `sudo apt install gir1.2-ayatanaappindicator3-0.1`.
- **"Already running" but no icon:** run `pkill -f claude_tray.py`, then start it again.
- **Resume says the conversation was not found:** the project folder was moved or deleted, and Claude Code finds sessions by folder.

## Roadmap

- Rename the command to `agent-tray` and split the Claude-specific code into a provider module
- More providers (other AI coding CLIs)
