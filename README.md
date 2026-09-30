# agent-tray

A Linux top-bar indicator for AI coding agents: **Claude Code** and **ChatGPT / Codex**. See every running session, the model it uses, how much context is left, token usage, time spent and your ChatGPT plan limits. Get silent popups when a task finishes or context runs low, and resume any session in one click without typing in a terminal.

> Unofficial community tool. Not affiliated with or endorsed by Anthropic or OpenAI. All icons (the ✨ AI sparkle, the orange spark and the teal hexagon) are drawn for this project and are not either company's logo.

## Quick start

```bash
git clone https://github.com/lincechacko-FSW/agent-tray.git
cd agent-tray
bash install.sh
```

The icon appears in the top bar. Click it to see your sessions. It starts automatically at every login.

## Supported agents

| Agent | What it reads | Running detection |
|---|---|---|
| **Claude Code** (CLI) | `~/.claude/sessions/` and `~/.claude/projects/` | Exact: one file per running process |
| **ChatGPT desktop app** (coding threads) | `~/.codex/state_*.sqlite` (read-only) and `~/.codex/sessions/` | **Busy** while a turn is running; **open** if the app is running and the thread was used in the last 30 min |
| **Codex CLI** | Same `~/.codex/` folder | Busy while a turn is running; open while a `codex` process runs in that folder |

ChatGPT **web / chat** conversations live on OpenAI's servers and are not shown. Only coding threads saved in `~/.codex` are.

## Features

### Each agent has its own colour and logo
| Agent | Colour | Logo | Where you see it |
|---|---|---|---|
| **Claude Code** | 🟠 orange | orange spark | menu section header, 🟠 before each session, orange gauge ring, card stripe, tint and logo |
| **ChatGPT / Codex** | 🟢 teal | teal hexagon | menu section header, 🟢 before each session, teal gauge ring, card stripe, tint, logo and button |

Busy / idle is shown separately, by ⚡ / 💤 in the menu and the status pill on each card.

### Top-bar icon
- **Always there:** a lavender ✨ AI sparkle in a faint orbit ring on a starry navy badge, with the number of running sessions next to it. It turns grey when nothing is running.
- **Space-themed animations** for every agent:

  | Event | Icon | Text next to icon (4 s) |
  |---|---|---|
  | New session opens | 🚀 A rocket launches through streaking stars, then the sparkle spins in | `🚀 poc-28 launched` |
  | A session starts working | 💥 The sparkle flares and a shock ring expands in the agent's colour (🟠 / 🟢) | `⚡ poc-28 working` |
  | Sessions busy | 🛰 A satellite orbits the sparkle in the busy agent's colour: 🟠 orange for Claude, 🟢 teal for ChatGPT, two satellites when both are busy | |
  | Session finishes a task | ✅ A green planet with a ✓ appears in a starburst ("mission complete") | `✓ poc-28 done` |
  | Session closed | 🌒 The sparkle sinks, greys out and fades | `✕ poc-28 closed` |
  | Context 80% used | Amber icon with a big **!** that pulses | `⚠ poc-28 ctx 82%` |
  | Context 95% used | Red icon with a big **!** that pulses | `⚠ poc-28 ctx 96%` |

- **Starts at login** automatically.

### Silent popups
| When | Popup |
|---|---|
| A task that ran 10 s or longer finishes | `✅ poc-28 finished` · `Claude Code · Took 2m 14s · opus-5-5 · 84% context left · ~/POC` |
| Claude stops to wait for you | `⏳ poc-28 needs attention` |
| Context 80% used | `⚠️ poc-28 context 82% full` · `consider /compact soon` |
| Context 95% used | `🔴 poc-28 context almost full` · `run /compact or start a new session` |
| ChatGPT 5-hour or weekly plan window passes 80% | `⚠️ ChatGPT 5-hour limit 82% used` · `Plan: plus · resets Tue 16:09` |

- No sound, and each popup has an **Open dashboard** button.
- A new popup for the same session replaces the previous one.
- Context warnings fire once per level per session, and reset after `/compact` or `/clear` drops usage below 70%.
- Plan-limit popups fire once per window per reset period.

### Tray menu (click the icon)
```
 ✨  Agent Tray — 3 running · 2 busy
     Today 535k tokens · 14.8M cached
 ───────────────────────────────────────────────────────
 [spark]    CLAUDE CODE  ·  2 running
 ◔  🟠  gyrodriver       💤 idle · 15h 11m          ▸
 ◕  🟠  agent-tray       ⚡ busy · 28m               ▸
 ───────────────────────────────────────────────────────
 [hexagon]  CHATGPT  ·  1 running  ·  plan 5h 5% · weekly 1% used
 ◔  🟢  Explain GNSS…    ⚡ busy · 28m               ▸
 ───────────────────────────────────────────────────────
 ↺  Resume a past session                           ▸     🟠 supervisor_main …   🟢 …
 ▦  Open dashboard…
 ↻  Refresh
 ───────────────────────────────────────────────────────
 ⏻  Quit
```
Hover over a session to open its submenu:
```
 🧠  gpt-6.1-sol
 ▰▰▰▰▰▰▱▱▱▱  64% context left
 ⬆ 83k in  ·  ⬇ 6.4k out  ·  869k cached
 ⏱  running 19m · active 7m
 📍  open in ChatGPT app
 ────────────
 ▶  Open copy in terminal
 💬  Open the ChatGPT app      (ChatGPT threads only)
 📁  Open folder
 📋  Copy resume command
```
- Sessions are grouped by agent under a header with the agent's logo. The ChatGPT header shows your plan usage.
- Each session has a **ring gauge icon** in its agent's colour, with the agent's mark inside. The ring fills with context used.
- **Resume a past session ▸** lists the last 10 ended sessions from both agents (🟠 Claude, 🟢 ChatGPT).

### Dashboard
A black window with an orange gradient header showing **Running**, **Busy**, **Today tokens** and **Cache reads**. Running sessions are grouped into a **Claude Code** section and a **ChatGPT** section, each with its logo and a count. The ChatGPT section also shows your plan usage (5-hour and weekly, with reset times). New cards slide in.

A switch under the header filters the dashboard to **All**, **🟠 Claude Code** or **🟢 ChatGPT**, with a live count on each button. It applies to both running and recently ended sessions, is instant, and is remembered the next time you open the dashboard (keys **1** / **2** / **3** work too).

Each card has its agent's colour: a coloured left stripe, a faint tint and the agent logo next to the name. Ended cards use a dimmer stripe.

Each card shows:
- agent logo, name, project folder, status pill (busy / idle / ended) and an agent label (**Claude Code** or **ChatGPT**)
- model(s) used, including subagent models, and whether it is a 1M-context model
- a context bar with **% left**
- tokens: input, output, cache read, cache write (Claude subagents included)
- time running, active time, last message, and API time and cost when available (Claude)

### Resume in one click
| Session | Button | Runs in a new terminal |
|---|---|---|
| Ended Claude session | **Open session** | `claude --resume <id>` |
| Running Claude session | **Open copy** | `claude --resume <id> --fork-session` (the original is untouched) |
| Ended ChatGPT / Codex thread | **Open session** | `codex resume <id>` |
| Open ChatGPT / Codex thread | **Open copy** | `codex fork <id>` (the original is untouched) |

ChatGPT cards also have a ↗ button that brings the ChatGPT app to the front.

### Light on resources
- Reads only the lines added to session files since the last check
- Reads the Codex database only when it changes, and opens it read-only
- Watches the Claude sessions folder for changes (inotify), so it updates the moment a session starts or stops
- Redraws only when something changed. Animation timers only run during an event animation or while a session is busy (the orbit), so the app uses no CPU when idle
- No pip dependencies

### Private by design
- Reads only the files Claude Code and ChatGPT / Codex already save in `~/.claude/` and `~/.codex/`
- Never reads `~/.codex/auth.json` or any credentials
- Makes no network requests and uses **no tokens**
- Session data never leaves your machine

## Supported systems

| System | Status |
|---|---|
| Ubuntu 22.04 / 24.04 (GNOME) | ✅ Tested, everything works |
| Ubuntu 25.04+ | ⚠️ Works, but **Open session** needs `gnome-terminal` (`sudo apt install gnome-terminal`), because the default terminal there is Ptyxis |
| Other GNOME distros (Fedora, Arch…) | ⚠️ Install the tray library and the AppIndicator extension yourself; `install.sh` only supports `apt` |
| KDE / XFCE / Cinnamon / MATE | ⚠️ Icon, menu and popups should work; **Open session** needs `gnome-terminal` installed |

### Requirements

- Linux with a desktop that shows AppIndicator / StatusNotifier tray icons. Tested on **Ubuntu 24.04 (GNOME, Wayland)**.
  - GNOME needs the **AppIndicator** extension. Ubuntu has it enabled by default (`ubuntu-appindicators@ubuntu.com`).
  - KDE, XFCE, Cinnamon and MATE support tray icons out of the box.
- Python 3.10+ with PyGObject (GTK 3). Preinstalled on Ubuntu.
- `gir1.2-ayatanaappindicator3-0.1`. `install.sh` installs it.
- `gnome-terminal`, used by the resume buttons.
- A desktop notification service for popups (built into GNOME, KDE and most desktops).
- At least one agent:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code), with sessions in `~/.claude/`
  - the ChatGPT desktop app or the Codex CLI (`npm i -g @openai/codex`), with threads in `~/.codex/`

## Install

```bash
git clone https://github.com/lincechacko-FSW/agent-tray.git
cd agent-tray
bash install.sh
```

`install.sh` will:
1. Install the tray-icon library with `apt` (asks for your sudo password)
2. Stop any running copy, and remove an old `claude-tray` install if there is one
3. Add an `agent-tray` command in `~/.local/bin`
4. Add an app-menu entry (**Agent Tray**) and start the app automatically at login
5. Start it now

> Autostart points at the folder you ran `install.sh` from. If you move the folder, run `bash install.sh` again.

## Usage

| Action | How |
|---|---|
| Start | `agent-tray`, or open **Agent Tray** from the app menu |
| See sessions | Click the icon in the top bar |
| See one session's details | Icon menu, then hover over the session |
| Open the dashboard | Icon menu, then **Open dashboard…**; or middle-click the icon; or click **Open dashboard** on a popup |
| Close the dashboard | ✕ at the top-right, or press **Esc** (the icon stays in the top bar) |
| Show only Claude or ChatGPT sessions | The **All / 🟠 Claude Code / 🟢 ChatGPT** switch under the dashboard header, or keys **1** / **2** / **3** |
| Resume an ended session | **Open session** on its card, or icon menu, then **Resume a past session ▸** |
| Open a running session | **Open copy** on its card, or icon menu, then session, then **Open copy in terminal** (a forked copy) |
| Bring up the ChatGPT app | ↗ on a ChatGPT card, the **CHATGPT** menu header, or **Open the ChatGPT app** in a thread's submenu |
| Open the project folder | 📁 on a card, or icon menu, then session, then **Open folder** |
| Copy the resume command | 📋 on a card, or icon menu, then session, then **Copy resume command** |
| Refresh now | ↻ in the dashboard title bar, or icon menu, then **Refresh** |
| Stop | Icon menu, then **Quit** |

Closing the terminal you started it from doesn't stop the app, and starting it twice won't add a second icon. Resume terminals always open as new windows, whichever way the app was started.

### Command-line options

```bash
agent-tray                # start in the background (default)
agent-tray --foreground   # stay attached to the terminal and print logs
agent-tray --dump         # print the current session data (both agents) as JSON and exit
```

Background logs go to `~/.cache/agent-tray/agent-tray.log`.

## Updating

```bash
cd agent-tray
git pull
bash install.sh
```

## Settings

Edit these constants in `agent_tray.py` (search for the name), then restart the app.

| Setting | Default | What it does |
|---|---|---|
| `NOTIFY_MIN_S` | `10` | Only show a finished-task popup for tasks that ran at least this many seconds |
| `LABEL_FLASH_S` | `4` | Seconds the event text stays next to the icon |
| `CTX_WARN` | `0.80` | Context used (fraction) that triggers the ⚠️ warning popup |
| `CTX_FULL` | `0.95` | Context used (fraction) that triggers the 🔴 almost-full popup |
| `CTX_REARM` | `0.70` | Warnings reset once context used drops below this |
| `LIMIT_WARN` | `0.80` | ChatGPT plan window usage (fraction) that triggers a popup |
| `CODEX_ACTIVE_S` | `1800` | A ChatGPT / Codex thread counts as open if used this recently while its app runs |
| `CODEX_STALE_S` | `600` | An unfinished ChatGPT / Codex turn with no activity for this long stops counting as busy |
| `POLL_S` | `3` | Seconds between checks of live sessions |
| `SLOW_S` | `30` | Seconds between rescans for ended Claude sessions and today's totals |
| `IDLE_GAP_S` | `300` | Gaps between messages longer than this don't count as active time |
| `ENDED_SHOWN` | `10` | Number of recently ended sessions to list |
| `ANIM_MS` | see file | Frame speed of each icon animation in milliseconds (`orbit` = busy satellite, default 200) |

## What the numbers mean

- **Context left:** the size of the last request compared with the model's context window (Claude: 200k, or 1M for `[1m]` models; ChatGPT: the window the app reports, e.g. 258k). It shows how full the conversation is, not your plan quota.
- **Active time:** the time between messages, not counting gaps longer than 5 minutes.
- **Took (popup):** how long the session was busy, from each agent's own timestamps.
- **Today:** tokens from all sessions of both agents since local midnight. Cache reads are shown separately because they are much larger and cheaper.
- **ChatGPT plan:** the 5-hour and weekly usage the ChatGPT app last reported, with reset times.
- **Claude plan limits** (5-hour and weekly) are not stored on your machine, so they can't be shown. Run `/usage` inside Claude Code to see them.

## How it works

**Claude Code** writes:
- `~/.claude/sessions/<pid>.json`: one file per open session, with name, folder, start time, and busy/idle status with a timestamp. The app checks each PID is still alive to spot stale files.
- `~/.claude/projects/<project>/<session-id>.jsonl`: each session's history, with the model and token usage of every response. Subagent histories are in `<session-id>/subagents/`.

**ChatGPT / Codex** writes:
- `~/.codex/state_*.sqlite`: the thread list (title, folder, model, last update). The app opens it read-only.
- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`: each thread's events. Turn start and finish give busy/idle; token events give usage, context window and plan limits.

A background thread reads both, and passes a snapshot to the GTK main thread only when something changed. The main thread compares each snapshot with the previous one to detect started, finished and closed sessions. It then plays the icon animation, shows the text next to the icon, and sends the popup through `org.freedesktop.Notifications` with sound turned off.

Icon frames are small SVGs generated once into `~/.cache/agent-tray/`.

## Uninstall

```bash
pkill -f "^(/usr/bin/)?python3 .*agent[-_]tray"
rm ~/.local/bin/agent-tray ~/.config/autostart/agent-tray.desktop ~/.local/share/applications/agent-tray.desktop
rm -rf ~/.cache/agent-tray
```

## Troubleshooting

- **No icon appears:** on GNOME, check the AppIndicator extension is enabled with `gnome-extensions list --enabled | grep -i appindicator`.
- **`Namespace AyatanaAppIndicator3 not available`:** run `sudo apt install gir1.2-ayatanaappindicator3-0.1`.
- **No popups:** check that **Do Not Disturb** is off, and that the task ran longer than `NOTIFY_MIN_S` seconds.
- **ChatGPT threads don't show:** check that `~/.codex/` exists and has a `state_*.sqlite` file, and run `agent-tray --dump` to see what the app reads.
- **"Already running" but no icon, or after an update:** run `bash install.sh`, or `pkill -f "^(/usr/bin/)?python3 .*agent[-_]tray"; agent-tray`.
- **Open session does nothing:** check that `gnome-terminal` is installed (`which gnome-terminal`), then look in `~/.cache/agent-tray/agent-tray.log` for the error.
- **Resume says the conversation was not found:** the project folder was moved or deleted, and Claude Code finds sessions by folder.
- **Anything else:** run `agent-tray --foreground` to see errors in the terminal. Quit the running copy first.

## Roadmap

- More agents (Gemini CLI, Aider…)
- Support more terminals (Ptyxis, Konsole, xfce4-terminal, Kitty…) for **Open session**
- `install.sh` support for `dnf` and `pacman`
