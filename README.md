# agent-tray

A Linux top-bar indicator for AI coding agents. See your running sessions, the model each one uses, how much context is left, token usage and time spent. Get a silent popup when a task finishes, and resume any session in one click without typing in a terminal.

Currently supports **Claude Code**. Support for other agents is planned.

> Unofficial community tool. Not affiliated with or endorsed by Anthropic.

## Quick start

```bash
git clone https://github.com/lincechacko-FSW/agent-tray.git
cd agent-tray
bash install.sh
```

The icon appears in the top bar. Click it to see your sessions.

## Features

### Top-bar icon
- **Always there:** an orange badge with the number of running sessions next to it. It is grey when nothing is running and has a green dot while any session is busy.
- **Animated on events:**

  | Event | Icon | Text next to icon (4 s) |
  |---|---|---|
  | New session opens | Badge pops in, spark spins with a flash | `+ poc-28 started` |
  | Session finishes a task | Whole icon turns green with a big ✓ and pulses | `✓ poc-28 done` |
  | Session closed | Grey icon with a big ✕ that fades out | `✕ poc-28 closed` |
  | Any session busy | Slow breathing glow + green dot | |

- **Starts at login** automatically.

### Popup when a task finishes
A silent desktop notification appears when a session finishes a task:
```
✅ poc-28 finished
Took 2m 14s · opus-5-5 · 84% context left · ~/POC      [Open dashboard]
```
- It only appears for tasks that ran **10 seconds or longer**, so quick replies don't spam you.
- A new popup for the same session replaces the previous one.
- If Claude stops to wait for you (e.g. a permission prompt), it says `⏳ poc-28 needs attention`.

### Tray menu (click the icon)
- A summary line: `2 running · 1 busy · today 815k tokens`
- One line per running session: `🟢 poc-28 — busy · opus-5-5 · ctx 84% left · 46m`
- **Open session ▸** submenu to resume a recently ended session
- **Open dashboard…**, **Refresh** and **Quit**

### Dashboard
A black window with an orange gradient header showing **Running**, **Busy**, **Today tokens** and **Cache reads**. It has one card per session. New cards slide in.

Each card shows:
- name, AI title, project folder and status pill (busy / idle / ended)
- model(s) used, including subagent models, and whether it is a 1M-context model
- a context bar with **% left**
- tokens: input, output, cache read, cache write (subagents included)
- time running, active time, last message, and API time and cost when available

Sessions are split into **Running** and **Recently ended** (the last 10).

### Resume in one click
- **Ended session:** **Open session** opens a new terminal in the project folder running `claude --resume <id>`.
- **Running session:** **Open copy** opens a forked copy (`--fork-session`) with the full history, so the original session is not touched.

### Light on resources
- Reads only the lines added to session files since the last check
- Watches the sessions folder for changes (inotify), so it updates the moment a session starts or stops
- Redraws only when something changed, and animation timers only run during an animation
- No pip dependencies

## Requirements

- Linux with a desktop that shows AppIndicator / StatusNotifier tray icons. Tested on **Ubuntu 24.04 (GNOME, Wayland)**.
  - GNOME needs the **AppIndicator** extension. Ubuntu has it enabled by default (`ubuntu-appindicators@ubuntu.com`).
  - KDE, XFCE, Cinnamon and MATE support tray icons out of the box.
- Python 3.10+ with PyGObject (GTK 3). Preinstalled on Ubuntu.
- `gir1.2-ayatanaappindicator3-0.1`. `install.sh` installs it.
- `gnome-terminal`, used by the resume buttons.
- A desktop notification service for popups (built into GNOME, KDE and most desktops).
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed, with sessions in `~/.claude/`.

## Install

```bash
git clone https://github.com/lincechacko-FSW/agent-tray.git
cd agent-tray
bash install.sh
```

`install.sh` will:
1. Install the tray-icon library with `apt` (asks for your sudo password)
2. Add a `claude-tray` command in `~/.local/bin`
3. Add an app-menu entry and start the app automatically at login
4. Start it now

> Autostart points at the folder you ran `install.sh` from. If you move the folder, run `bash install.sh` again.

## Usage

| Action | How |
|---|---|
| Start | `claude-tray`, or open **Claude Tray** from the app menu |
| See sessions | Click the icon in the top bar |
| Open the dashboard | Icon menu, then **Open dashboard…**; or middle-click the icon; or click **Open dashboard** on a popup |
| Close the dashboard | ✕ at the top-right, or press **Esc** (the icon stays in the top bar) |
| Resume an ended session | **Open session** on its card, or icon menu, then **Open session ▸** |
| Open a running session | **Open copy** on its card (forked copy in a new terminal) |
| Open the project folder | 📁 folder icon on a card |
| Copy the resume command | 📋 copy icon on a card |
| Refresh now | ↻ in the dashboard title bar, or icon menu, then **Refresh** |
| Stop | Icon menu, then **Quit** |

Closing the terminal you started it from doesn't stop the app, and starting it twice won't add a second icon.

### Command-line options

```bash
claude-tray                # start in the background (default)
claude-tray --foreground   # stay attached to the terminal and print logs
claude-tray --dump         # print the current session data as JSON and exit
```

Background logs go to `~/.cache/claude-tray/claude-tray.log`.

## Updating

```bash
cd agent-tray
git pull
pkill -f "^(/usr/bin/)?python3 .*claude[-_]tray"; claude-tray
```

## Settings

Edit these constants in `claude_tray.py` (search for the name), then restart the app.

| Setting | Default | What it does |
|---|---|---|
| `NOTIFY_MIN_S` | `10` | Only show a popup for tasks that ran at least this many seconds |
| `LABEL_FLASH_S` | `4` | Seconds the event text stays next to the icon |
| `POLL_S` | `3` | Seconds between checks of live sessions |
| `SLOW_S` | `30` | Seconds between rescans for ended sessions and today's totals |
| `IDLE_GAP_S` | `300` | Gaps between messages longer than this don't count as active time |
| `ENDED_SHOWN` | `10` | Number of recently ended sessions to list |
| `ANIM_MS` | see file | Frame speed of each icon animation in milliseconds |

## What the numbers mean

- **Context left:** the size of the last request (input + cache + output) compared with the model's context window (200k, or 1M for `[1m]` models). It shows how full the conversation is, not your plan quota.
- **Active time:** the time between messages, not counting gaps longer than 5 minutes.
- **Took (popup):** how long the session was busy, from Claude Code's own status timestamps.
- **Today:** tokens from all sessions since local midnight. Cache reads are shown separately because they are much larger and cheaper.
- **Plan usage limits** (5-hour and weekly) are not stored on your machine, so they can't be shown. Run `/usage` inside Claude Code to see them.

## How it works

Claude Code writes everything the app needs to disk:

- `~/.claude/sessions/<pid>.json`: one file per open session, with name, folder, start time, and busy/idle status with a timestamp. The app checks each PID is still alive to spot stale files.
- `~/.claude/projects/<project>/<session-id>.jsonl`: each session's history, with the model and token usage of every response. Subagent histories are in `<session-id>/subagents/`.

A background thread reads these files and passes a snapshot to the GTK main thread only when something changed. The main thread compares each snapshot with the previous one to detect started, finished and closed sessions. It then plays the icon animation, shows the text next to the icon, and sends the popup through `org.freedesktop.Notifications` with sound turned off.

Icon frames are small SVGs generated once into `~/.cache/claude-tray/`.

## Uninstall

```bash
pkill -f "^(/usr/bin/)?python3 .*claude[-_]tray"
rm ~/.local/bin/claude-tray ~/.config/autostart/claude-tray.desktop ~/.local/share/applications/claude-tray.desktop
rm -rf ~/.cache/claude-tray
```

## Troubleshooting

- **No icon appears:** on GNOME, check the AppIndicator extension is enabled with `gnome-extensions list --enabled | grep -i appindicator`.
- **`Namespace AyatanaAppIndicator3 not available`:** run `sudo apt install gir1.2-ayatanaappindicator3-0.1`.
- **No popups:** check that **Do Not Disturb** is off, and that the task ran longer than `NOTIFY_MIN_S` seconds.
- **"Already running" but no icon, or after an update:** run `pkill -f "^(/usr/bin/)?python3 .*claude[-_]tray"; claude-tray`.
- **Resume says the conversation was not found:** the project folder was moved or deleted, and Claude Code finds sessions by folder.
- **Anything else:** run `claude-tray --foreground` to see errors in the terminal. Quit the running copy first.

## Roadmap

- Rename the command to `agent-tray` and split the Claude-specific code into a provider module
- More providers (other AI coding CLIs)
