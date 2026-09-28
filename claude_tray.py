#!/usr/bin/env python3
"""claude-tray: top-bar indicator listing Claude Code sessions with status, models, tokens and timing."""
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import signal
import sys
import threading
import time
from datetime import datetime

HOME = os.path.expanduser('~')
CLAUDE_DIR = os.path.join(HOME, '.claude')
SESS_DIR = os.path.join(CLAUDE_DIR, 'sessions')
PROJ_DIR = os.path.join(CLAUDE_DIR, 'projects')
CACHE_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache')), 'claude-tray')

POLL_S = 3            # fast tick: live sessions + their transcripts
SLOW_S = 30           # slow tick: rescan all projects for ended / today's files
UI_TICK_S = 30        # re-render elapsed times even when data is unchanged
IDLE_GAP_S = 300      # gaps between messages longer than this aren't "active" time
ENDED_SHOWN = 10
CTX_STD, CTX_1M = 200_000, 1_000_000


def _ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


class Transcript:
    """A session .jsonl parsed incrementally: each update() reads only bytes appended since the last one."""
    __slots__ = ('path', 'offset', 'tok', 'day', 'model', 'models', 'ctx', 'window', 'cwd',
                 'first_ts', 'last_ts', 'active', 'title', 'ai_title', 'cost', 'api_ms',
                 '_mid', '_pu', '_pday')

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.tok = [0, 0, 0, 0]      # input, output, cache read, cache write
        self.day = {}                # (y, m, d) -> [input+output+cache write, cache read]
        self.model = self.cwd = self.title = self.ai_title = self.cost = self.api_ms = None
        self.models = {}             # insertion-ordered set
        self.ctx = 0
        self.window = CTX_STD
        self.first_ts = self.last_ts = None
        self.active = 0.0
        self._mid = self._pu = self._pday = None

    def update(self):
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return
        if size < self.offset:       # file was rewritten, start over
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            for line in f:
                if line[-1:] != b'\n':   # line still being written
                    break
                self.offset += len(line)
                self._feed(line)

    def _feed(self, line):
        # Cheap byte prefilter so most lines (tool output, snapshots) are never JSON-decoded.
        if not (b'"type":"assistant"' in line or b'"type":"user"' in line or b'"identity"' in line
                or b'-title"' in line or b'"cost-state"' in line):
            return
        try:
            d = json.loads(line)
        except ValueError:
            return
        t = d.get('type')
        if t == 'user' or t == 'assistant':
            ts = _ts(d.get('timestamp'))
            if ts:
                if self.last_ts and 0 < ts - self.last_ts < IDLE_GAP_S:
                    self.active += ts - self.last_ts
                if self.first_ts is None:
                    self.first_ts = ts
                self.last_ts = max(ts, self.last_ts or 0)
            if self.cwd is None:
                self.cwd = d.get('cwd')
            if t == 'assistant':
                self._assistant(d, ts)
        elif t == 'attachment':
            ident = (d.get('attachment') or {}).get('identity')
            if ident:
                self.window = CTX_1M if '[1m]' in (ident.get('modelId') or '') else CTX_STD
        elif t == 'custom-title':
            self.title = d.get('customTitle')
        elif t == 'ai-title':
            self.ai_title = d.get('aiTitle')
        elif t == 'cost-state':
            self.cost, self.api_ms = d.get('totalCostUSD'), d.get('totalAPIDuration')

    def _assistant(self, d, ts):
        m = d.get('message') or {}
        side = d.get('isSidechain')
        model = m.get('model')
        if model and model != '<synthetic>':
            self.models[model] = None
            if not side:
                self.model = model
        u = m.get('usage')
        if not u:
            return
        u4 = [u.get('input_tokens') or 0, u.get('output_tokens') or 0,
              u.get('cache_read_input_tokens') or 0, u.get('cache_creation_input_tokens') or 0]
        day = time.localtime(ts)[:3] if ts else None
        mid = m.get('id')
        # One response is streamed across several lines sharing an id; keep only the latest usage.
        if mid and mid == self._mid:
            self._add(self._pu, self._pday, -1)
        self._mid, self._pu, self._pday = mid, u4, day
        self._add(u4, day, 1)
        if not side:
            self.ctx = sum(u4)

    def _add(self, u4, day, sign):
        tok = self.tok
        for i in range(4):
            tok[i] += sign * u4[i]
        if day:
            b = self.day.setdefault(day, [0, 0])
            b[0] += sign * (u4[0] + u4[1] + u4[3])
            b[1] += sign * u4[2]


def _proc_start(pid):
    try:
        with open(f'/proc/{int(pid)}/stat', 'rb') as f:
            return f.read().rsplit(b')', 1)[1].split()[19].decode()
    except (OSError, IndexError, TypeError, ValueError):
        return None


class Monitor:
    """Background scanner; pushes a plain-dict snapshot to on_snapshot whenever something changed."""

    def __init__(self, on_snapshot=None):
        self.on_snapshot = on_snapshot
        self.wake = threading.Event()
        self.force = True
        self.cache = {}          # transcript path -> Transcript
        self.meta = {}           # session json path -> (mtime_ns, dict)
        self.paths = {}          # sessionId -> transcript path
        self.recent = []         # newest transcript paths (ended-session candidates)
        self.today = []          # transcript paths touched today
        self.last_slow = 0
        self._last = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def poke(self, force=False):
        self.force = self.force or force
        self.wake.set()

    def _run(self):
        while True:
            try:
                snap = self.scan()
                if snap != self._last:
                    self._last = snap
                    self.on_snapshot(snap)
            except Exception as e:  # keep the tray alive on unexpected data
                print('scan error:', repr(e), file=sys.stderr)
            self.wake.wait(POLL_S)
            self.wake.clear()

    def _get(self, path):
        t = self.cache.get(path)
        if t is None:
            t = self.cache[path] = Transcript(path)
        t.update()
        return t

    def _live(self):
        live, seen = [], set()
        try:
            names = os.listdir(SESS_DIR)
        except OSError:
            names = []
        for n in names:
            if not n.endswith('.json'):
                continue
            p = os.path.join(SESS_DIR, n)
            seen.add(p)
            try:
                mt = os.stat(p).st_mtime_ns
            except OSError:
                continue
            c = self.meta.get(p)
            if c is None or c[0] != mt:
                try:
                    with open(p) as f:
                        c = self.meta[p] = (mt, json.load(f))
                except (OSError, ValueError):
                    continue
            s = c[1]
            # procStart guards against a stale file whose PID was reused by another process.
            st, want = _proc_start(s.get('pid')), s.get('procStart')
            if st is not None and (want is None or st == str(want)):
                live.append(s)
        for p in self.meta.keys() - seen:
            del self.meta[p]
        return live

    def _path_for(self, s):
        sid = s.get('sessionId') or ''
        p = self.paths.get(sid)
        if p is None:
            p = os.path.join(PROJ_DIR, re.sub(r'[^A-Za-z0-9]', '-', s.get('cwd') or ''), sid + '.jsonl')
            if not os.path.exists(p):
                hits = glob.glob(os.path.join(PROJ_DIR, '*', sid + '.jsonl'))
                if not hits:
                    return None      # no messages yet; retry next tick
                p = hits[0]
            self.paths[sid] = p
        return p

    @staticmethod
    def _subagents(path):
        d = os.path.join(path[:-6], 'subagents')
        try:
            return [os.path.join(d, n) for n in os.listdir(d) if n.endswith('.jsonl')]
        except OSError:
            return []

    def _rescan_history(self):
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        files = []
        for p in glob.glob(os.path.join(PROJ_DIR, '*', '*.jsonl')):
            try:
                files.append((os.stat(p).st_mtime, p))
            except OSError:
                pass
        files.sort(reverse=True)
        self.recent = [p for _, p in files[:ENDED_SHOWN * 2]]
        today = [p for m, p in files if m >= midnight]
        for p in glob.glob(os.path.join(PROJ_DIR, '*', '*', 'subagents', '*.jsonl')):
            try:
                if os.stat(p).st_mtime >= midnight:
                    today.append(p)
            except OSError:
                pass
        self.today = today

    def _row(self, t, subs, s=None):
        sid = s.get('sessionId') if s else os.path.basename(t.path)[:-6]
        tok, models = [0, 0, 0, 0], {}
        for x in ([t] if t else []) + subs:
            for i in range(4):
                tok[i] += x.tok[i]
            models.update(x.models)
        ctx = t.ctx if t else 0
        window = t.window if t else CTX_STD
        if ctx > window:
            window = CTX_1M
        title = t and (t.title or t.ai_title)
        status = (s.get('status') or 'idle') if s else 'ended'
        s = s or {}
        name = s.get('name') or title or sid[:8]
        return {
            'sid': sid,
            'name': name,
            'title': title if title != name else None,
            'cwd': s.get('cwd') or (t and t.cwd) or '',
            'pid': s.get('pid'),
            'status': status,
            'started': (s.get('startedAt') or 0) / 1000 or (t and t.first_ts),
            'since': (s.get('statusUpdatedAt') or 0) / 1000 or None,
            'last': t and t.last_ts,
            'active': t.active if t else 0,
            'model': t and t.model,
            'models': list(models),
            'tok': tok,
            'ctx': ctx,
            'window': window,
            'cost': t and t.cost,
            'api_ms': t and t.api_ms,
        }

    def scan(self):
        now = time.time()
        live = sorted(self._live(), key=lambda s: s.get('startedAt') or 0)
        slow = self.force or now - self.last_slow >= SLOW_S
        if slow:
            self.force, self.last_slow = False, now
            self._rescan_history()

        keep, rows, live_sids = set(), [], set()
        for s in live:
            live_sids.add(s.get('sessionId'))
            p = self._path_for(s)
            subs = self._subagents(p) if p else []
            keep.update(subs)
            if p:
                keep.add(p)
            rows.append(self._row(self._get(p) if p else None, [self._get(q) for q in subs], s))

        ended = []
        for p in self.recent:
            if os.path.basename(p)[:-6] in live_sids:
                continue
            subs = self._subagents(p)
            keep.add(p)
            keep.update(subs)
            ended.append(self._row(self._get(p), [self._get(q) for q in subs]))
            if len(ended) == ENDED_SHOWN:
                break

        if slow:
            for p in self.today:
                self._get(p)
            keep.update(self.today)
            for p in self.cache.keys() - keep:
                del self.cache[p]

        day = time.localtime(now)[:3]
        io = cr = 0
        for t in self.cache.values():
            b = t.day.get(day)
            if b:
                io += b[0]
                cr += b[1]
        return {'live': rows, 'ended': ended, 'today': [io, cr]}


# ---------------------------------------------------------------- startup (before GTK is loaded)

def _single_instance():
    os.makedirs(CACHE_DIR, exist_ok=True)
    lock = open(os.path.join(CACHE_DIR, 'lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('claude-tray is already running (look for the icon in the top bar).')
        sys.exit(0)
    return lock


def _detach():
    # Fork into the background so closing the terminal doesn't remove the icon.
    if os.fork():
        os._exit(0)
    os.setsid()
    null = os.open(os.devnull, os.O_RDONLY)
    log = os.open(os.path.join(CACHE_DIR, 'claude-tray.log'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(null, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)


if __name__ == '__main__':
    if '--dump' in sys.argv:
        print(json.dumps(Monitor().scan(), indent=1))
        sys.exit(0)
    _LOCK = _single_instance()
    if '--foreground' not in sys.argv:
        _detach()

import gi  # noqa: E402  (imported after fork so the child owns the display connection)
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import AyatanaAppIndicator3 as AI, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

ORANGE, GREY, GREEN_BG, GREEN = ('#F0A077', '#C95F3C'), ('#B9B5AD', '#77736B'), ('#5BE38F', '#1C9A52'), '#34C26E'
ANIM_MS = {'spawn': 80, 'done': 120, 'close': 110, 'breathe': 500}
NOTIFY_MIN_S = 10     # only popup for tasks that ran at least this long
LABEL_FLASH_S = 4     # how long an event message stays next to the icon

GLYPHS = {
    'spark': ''.join(f'<line x1="16" y1="5.5" x2="16" y2="26.5" transform="rotate({a} 16 16)"/>'
                     for a in (0, 45, 90, 135)),
    'check': '<path d="M8 16.8 L13.2 22 L24 10.4" fill="none" stroke-linejoin="round"/>',
    'cross': '<path d="M10 10 L22 22 M22 10 L10 22"/>',
}


def _mix(a, b, t):
    pa, pb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return '#%02x%02x%02x' % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def icon_svg(grad=ORANGE, glyph='spark', scale=1.0, rot=0.0, pop=1.0, flash=0.0, fade=1.0, dot=None, dot_a=1.0):
    """Edge-to-edge badge with a bold white glyph; pop scales the badge, scale/rot the glyph."""
    s = ['<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
         '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
         f'<stop offset="0" stop-color="{grad[0]}"/><stop offset="1" stop-color="{grad[1]}"/>'
         f'</linearGradient></defs><g opacity="{fade:.2f}" '
         f'transform="translate(16 16) scale({pop:.3f}) translate(-16 -16)">'
         '<rect x="0" y="0" width="32" height="32" rx="8" fill="url(#g)"/>']
    if flash:
        s.append(f'<rect x="0" y="0" width="32" height="32" rx="8" fill="#fff" opacity="{flash:.2f}"/>')
    s.append(f'<g stroke="#fff" stroke-width="4.4" stroke-linecap="round" '
             f'transform="translate(16 16) rotate({rot:.1f}) scale({scale:.3f}) translate(-16 -16)">'
             f'{GLYPHS[glyph]}</g></g>')
    if dot:
        s.append(f'<circle cx="25" cy="25" r="6.5" fill="{dot}" stroke="#111" stroke-width="1.8" '
                 f'opacity="{dot_a:.2f}"/>')
    s.append('</svg>')
    return ''.join(s)


def icon_frames():
    return {
        'none': [icon_svg(GREY)],
        'idle': [icon_svg()],
        'busy': [icon_svg(dot=GREEN)],
        # new session: badge pops in, spark spins and overshoots under a fading flash
        'spawn': [icon_svg(pop=p, scale=s, rot=r, flash=f) for p, s, r, f in
                  ((.55, .4, -90, .7), (.7, .6, -65, .6), (.85, .85, -40, .5), (.95, 1.1, -20, .4),
                   (1, 1.2, -8, .3), (1, 1.1, 0, .2), (1, 1.0, 0, .1), (1, 1, 0, 0))],
        # task finished: whole icon turns green with a big check, pulses twice, holds
        'done': [icon_svg(GREEN_BG, 'check', pop=p, scale=s, flash=f) for p, s, f in
                 ((.55, .5, 0), (.75, .8, 0), (.95, 1.15, 0), (1, 1.05, 0), (1, 1, 0), (1, 1, .45), (1, 1, 0),
                  (1, 1, .45), (1, 1, 0), (1, 1, 0), (1, 1, 0), (1, 1, 0), (1, 1, 0))],
        # session closed: grey badge with a big cross, then fades out
        'close': [icon_svg(GREY, 'cross', pop=p, scale=s, fade=f) for p, s, f in
                  ((.6, .6, 1), (.85, 1.1, 1), (1, 1, 1), (1, 1, 1), (1, 1, 1), (1, 1, .85), (1, 1, .7),
                   (1, 1, .55), (1, 1, .4))],
        'breathe': [icon_svg(scale=s, dot=GREEN, dot_a=a) for s, a in
                    ((1.0, 1.0), (1.07, .75), (1.12, .5), (1.07, .75))],
    }


class Notifier:
    """Silent desktop popups over org.freedesktop.Notifications; one replaceable popup per session."""

    def __init__(self, on_action):
        self.on_action = on_action
        self.ids = {}                    # session key -> notification id
        self.proxy = None
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
                'org.freedesktop.Notifications', None)
            self.proxy.connect('g-signal', self._signal)
        except GLib.Error as e:
            print('notifications unavailable:', e.message, file=sys.stderr)

    def notify(self, key, title, body, icon):
        if not self.proxy:
            return
        hints = {'suppress-sound': GLib.Variant('b', True),
                 'desktop-entry': GLib.Variant('s', 'claude-tray')}
        args = GLib.Variant('(susssasa{sv}i)', ('Claude Tray', self.ids.get(key, 0), icon, title, body,
                                                ['default', 'Open dashboard', 'dash', 'Open dashboard'],
                                                hints, -1))
        self.proxy.call('Notify', args, Gio.DBusCallFlags.NONE, -1, None, self._sent, key)

    def _sent(self, proxy, res, key):
        try:
            self.ids[key] = proxy.call_finish(res)[0]
        except GLib.Error as e:
            print('notify failed:', e.message, file=sys.stderr)

    def _signal(self, _proxy, _sender, signal, params):
        if signal == 'ActionInvoked' and params[0] in self.ids.values():
            self.on_action()


class Animator:
    """Plays queued icon frame sequences, then returns to the resting icon (or a slow busy breathe)."""

    def __init__(self, ind):
        self.ind = ind
        self.queue = []
        self.timer = None
        self.frames, self.i, self.loop = [], 0, False
        self.rest, self.breathe = 'none', False
        self._shown = None

    def _show(self, name):
        if name != self._shown:
            self._shown = name
            self.ind.set_icon_full(f'claude-tray-{name}', 'Claude sessions')

    def set_rest(self, rest, breathe):
        changed = (rest, breathe) != (self.rest, self.breathe)
        self.rest, self.breathe = rest, breathe
        if self.timer is None or (changed and self.loop):
            self._next()

    def play(self, anim):
        if len(self.queue) < 4 and anim not in self.queue:
            self.queue.append(anim)
        if self.timer is None or self.loop:     # interrupt breathing, never a one-shot
            self._next()

    def _next(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.queue:
            anim, self.loop = self.queue.pop(0), False
        elif self.breathe:
            anim, self.loop = 'breathe', True
        else:
            self._show(f'{self.rest}-0')
            return
        self.frames = [f'{anim}-{i}' for i in range(len(ICON_FRAMES[anim]))]
        self.i = 0
        self._step()
        self.timer = GLib.timeout_add(ANIM_MS[anim], self._step)

    def _step(self):
        if self.i >= len(self.frames):
            if self.loop and not self.queue:
                self.i = 0
            else:
                self.timer = None
                self._next()
                return False
        self._show(self.frames[self.i])
        self.i += 1
        return True


ICON_FRAMES = icon_frames()


STATUS = {'busy': ('#4CAF50', '●', '🟢'), 'idle': ('#E0A030', '●', '🟡'), 'ended': ('#888888', '○', '⚪')}
CSS = b"""
window.dash, window.dash viewport, .content { background-color: #000000; color: #F2F2F2; }
window.dash headerbar { background-image: none; background-color: #0A0A0A; color: #F2F2F2;
    border-bottom: 1px solid #1C1C1C; box-shadow: none; }
window.dash headerbar button.titlebutton { color: #D4D4D4; border-radius: 999px; min-width: 24px; min-height: 24px;
    padding: 2px; background-image: none; background-color: #1C1C1C; border: none; box-shadow: none; }
window.dash headerbar button.titlebutton:hover { background-color: #2A2A2A; color: #FFFFFF; }
window.dash headerbar button.titlebutton.close { background-color: #E0784F; color: #FFFFFF; }
window.dash headerbar button.titlebutton.close:hover { background-color: #F08A60; }
.banner { background-image: linear-gradient(135deg, #E8855A, #B0432A); border-radius: 16px;
    padding: 16px 16px 14px 16px; color: #FFFFFF; box-shadow: 0 6px 22px rgba(224, 120, 79, 0.28); }
.kicker { font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; color: rgba(255, 255, 255, 0.85); }
.hero { font-size: 17pt; font-weight: 800; }
.tile { background-color: rgba(0, 0, 0, 0.20); border-radius: 12px; padding: 8px 10px; }
.tile-val { font-size: 15pt; font-weight: 800; }
.tile-key { font-size: 8.5pt; color: rgba(255, 255, 255, 0.85); }
.section { font-size: 10.5pt; font-weight: 700; color: #F2F2F2; }
.count { background-color: #2A160E; color: #F0916A; border-radius: 999px; padding: 0 8px;
    font-size: 8.5pt; font-weight: 700; }
.card { background-color: #0F0F0F; border: 1px solid #222222; border-left: 4px solid #3A3A3A;
    border-radius: 14px; padding: 12px 14px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.6); }
.card.busy { border-left-color: #4ADE80; }
.card.idle { border-left-color: #FBBF24; }
.name { font-size: 12pt; font-weight: 700; color: #F2F2F2; }
.muted { color: #A3A3A3; }
.faint { color: #6E6E6E; font-size: 9pt; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 8.5pt; font-weight: 700;
    background-color: #1C1C1C; color: #9A9A9A; }
.pill.busy { background-color: #0E2A19; color: #4ADE80; }
.pill.idle { background-color: #2B2210; color: #FBBF24; }
.chip { background-color: #1A1A1A; color: #D4D4D4; border: 1px solid #2A2A2A; border-radius: 6px;
    padding: 1px 8px; font-size: 8.5pt; font-weight: 600; font-family: monospace; }
.pct { font-size: 13pt; font-weight: 800; color: #F2F2F2; }
.card progressbar trough { min-height: 8px; border-radius: 4px; background-color: #1F1F1F; border: none; }
.card progressbar progress { min-height: 8px; border-radius: 4px; border: none;
    background-image: linear-gradient(to right, #22A35A, #4ADE80); }
.card progressbar.warn progress { background-image: linear-gradient(to right, #D99A12, #FBBF24); }
.card progressbar.crit progress { background-image: linear-gradient(to right, #C8372D, #F87171); }
.stat { background-color: #070707; border: 1px solid #1F1F1F; border-radius: 10px; padding: 6px 8px; }
.stat-val { font-weight: 700; color: #F2F2F2; }
.stat-key { color: #6E6E6E; font-size: 8pt; }
button.primary { background-image: none; background-color: #E0784F; color: #FFFFFF; border: none;
    border-radius: 999px; padding: 4px 16px; font-weight: 700; box-shadow: 0 2px 10px rgba(224, 120, 79, 0.35); }
button.primary:hover { background-color: #F08A60; }
button.primary label { color: #FFFFFF; }
button.icon { background-image: none; background-color: transparent; border: none; box-shadow: none;
    border-radius: 999px; padding: 4px 6px; color: #A3A3A3; }
button.icon:hover { background-color: #1F1F1F; color: #FFFFFF; }
.empty { background-color: #0A0A0A; border: 1px dashed #2A2A2A; border-radius: 14px; padding: 18px; color: #A3A3A3; }
"""


def fmt_tok(n):
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'k')):
        if n >= div:
            return f'{n / div:.1f}'.rstrip('0').rstrip('.') + suf
    return str(int(n))


def fmt_dur(s):
    s = int(max(s or 0, 0))
    if s < 60:
        return f'{s}s'
    m = s // 60
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f'{d}d {h}h' if d else f'{h}h {m:02d}m' if h else f'{m}m'


def short_model(m):
    return (m or '?').removeprefix('claude-')


def esc(s):
    return GLib.markup_escape_text(str(s or ''))


CLAUDE_BIN = shutil.which('claude') or os.path.join(HOME, '.local', 'bin', 'claude')
TERMINAL = shutil.which('gnome-terminal') or 'x-terminal-emulator'


def resume_cmd(r, fork=False):
    return (f'{shlex.quote(CLAUDE_BIN)} --resume {shlex.quote(r["sid"])}'
            + (' --fork-session' if fork else ''))


def launch_session(r, fork=False):
    # New terminal in the session's folder running claude --resume; the shell stays after claude exits.
    cwd = r['cwd'] if os.path.isdir(r['cwd']) else HOME
    argv = [TERMINAL, f'--working-directory={cwd}', '--', 'bash', '-lc', f'{resume_cmd(r, fork)}; exec bash']
    try:
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
    except GLib.Error as e:
        print('could not open terminal:', e.message, file=sys.stderr)


def apply_theme():
    # Dark Yaru for this process only, so scrollbars and arrows match the black palette in any mode.
    st = Gtk.Settings.get_default()
    st.set_property('gtk-application-prefer-dark-theme', True)
    if os.path.isdir('/usr/share/themes/Yaru-dark'):
        st.set_property('gtk-theme-name', 'Yaru-dark')
    css = Gtk.CssProvider()
    css.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _lbl(*classes, ellipsize=None, wrap=False, **kw):
    lb = Gtk.Label(xalign=0, **kw)
    lb.set_line_wrap(wrap)
    if ellipsize:
        lb.set_ellipsize(ellipsize)
    _cls(lb, *classes)
    return lb


def _cls(widget, *classes):
    sc = widget.get_style_context()
    for c in classes:
        sc.add_class(c)
    return widget


def _box(*children, vertical=False, spacing=0, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                spacing=spacing, **kw)
    for c in children:
        b.pack_start(c, False, False, 0)
    return b


def _set(widget, markup):
    if widget.get_label() != markup:     # avoid needless relayout
        widget.set_markup(markup)


def _icon_btn(icon, tip, cb):
    b = _cls(Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON), 'icon')
    b.set_tooltip_text(tip)
    b.connect('clicked', cb)
    return b


class Card(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _cls(self, 'card')
        self.row = self._state = None
        E = Pango.EllipsizeMode
        self.name = _lbl('name', ellipsize=E.END)
        self.title = _lbl('muted', ellipsize=E.END, no_show_all=True)
        self.folder = _lbl('faint', ellipsize=E.MIDDLE)
        self.pill = _lbl('pill', valign=Gtk.Align.START)
        names = _box(self.name, self.title, self.folder, vertical=True, spacing=1)
        top = Gtk.Box(spacing=10)
        top.pack_start(names, True, True, 0)
        top.pack_end(self.pill, False, False, 0)

        self.chip = _lbl('chip')
        self.also = _lbl('faint', ellipsize=E.END, no_show_all=True)
        models = _box(self.chip, self.also, spacing=8)

        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
        self.pct = _lbl('pct')
        ctx = Gtk.Box(spacing=10)
        ctx.pack_start(self.bar, True, True, 0)
        ctx.pack_end(self.pct, False, False, 0)
        self.ctx_note = _lbl('faint')

        stats = Gtk.Box(spacing=6, homogeneous=True)
        self.stats = []
        for key in ('Input', 'Output', 'Cache read', 'Cache write'):
            val = _lbl('stat-val')
            stats.pack_start(_cls(_box(val, _lbl('stat-key', label=key), vertical=True), 'stat'), True, True, 0)
            self.stats.append(val)

        self.time = _lbl('muted', wrap=True)
        self.open_btn = _cls(Gtk.Button(label='Open session'), 'primary')
        self.open_btn.connect('clicked', self._resume)
        actions = _box(self.open_btn,
                       _icon_btn('folder-open-symbolic', 'Open project folder', self._open),
                       _icon_btn('edit-copy-symbolic', 'Copy resume command', self._copy), spacing=4)
        for w in (top, models, ctx, self.ctx_note, stats, self.time, actions):
            self.pack_start(w, False, False, 0)
        self.show_all()

    def set(self, r, now):
        self.row = r
        st = r['status'] if r['status'] in STATUS else 'idle'
        if st != self._state:
            sc, pc = self.get_style_context(), self.pill.get_style_context()
            if self._state:
                sc.remove_class(self._state)
                pc.remove_class(self._state)
            sc.add_class(st)
            pc.add_class(st)
            self._state = st
        live = r['status'] != 'ended'
        _set(self.name, esc(r['name']))
        _set(self.title, esc(r['title']))
        self.title.set_visible(bool(r['title']))
        _set(self.folder, esc(r['cwd'].replace(HOME, '~', 1))
             + (f'  ·  PID {r["pid"]}, open in a terminal' if live else ''))
        _set(self.pill, f'{STATUS[st][1]} {esc(r["status"])}')

        cur = r['model']
        others = [short_model(m) for m in r['models'] if m != cur]
        _set(self.chip, esc(short_model(cur)) + (' · 1M' if r['window'] == CTX_1M else ''))
        _set(self.also, f'also {esc(", ".join(others))}' if others else '')
        self.also.set_visible(bool(others))

        frac = min(r['ctx'] / r['window'], 1.0)
        self.bar.set_fraction(frac)
        bc = self.bar.get_style_context()
        for cls, on in (('warn', 0.7 <= frac < 0.9), ('crit', frac >= 0.9)):
            (bc.add_class if on else bc.remove_class)(cls)
        _set(self.pct, f'{1 - frac:.0%} <span size="small" weight="normal" foreground="#6E6E6E">left</span>')
        _set(self.ctx_note, f'{fmt_tok(r["ctx"])} of {fmt_tok(r["window"])} context used')
        for lb, v in zip(self.stats, r['tok']):
            _set(lb, fmt_tok(v))

        if live:
            parts = [f'Running <b>{fmt_dur(now - r["started"]) if r["started"] else "?"}</b>',
                     f'Active <b>{fmt_dur(r["active"])}</b>']
            if r['last']:
                parts.append(f'Last message {fmt_dur(now - r["last"])} ago')
        else:
            parts = [f'Active <b>{fmt_dur(r["active"])}</b>']
            if r['last']:
                parts.append(f'Last used {fmt_dur(now - r["last"])} ago')
        if r['api_ms']:
            parts.append(f'API {fmt_dur(r["api_ms"] / 1000)}')
        if r['cost']:
            parts.append(f'<b>${r["cost"]:.2f}</b>')
        _set(self.time, f'<small>{"  ·  ".join(parts)}</small>')

        label = 'Open copy' if live else 'Open session'
        if self.open_btn.get_label() != label:
            self.open_btn.set_label(label)
            self.open_btn.set_tooltip_text(
                'Already running elsewhere: opens a forked copy with the full history in a new terminal'
                if live else 'Resume this session in a new terminal')

    def _resume(self, _b):
        if self.row:
            launch_session(self.row, fork=self.row['status'] != 'ended')

    def _open(self, _b):
        if self.row and os.path.isdir(self.row['cwd']):
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(self.row['cwd']), None)

    def _copy(self, _b):
        if self.row:
            cmd = f'cd {shlex.quote(self.row["cwd"])} && {resume_cmd(self.row)}'
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(cmd, -1)


def _section(title):
    count = _lbl('count', valign=Gtk.Align.CENTER)
    return _box(_lbl('section', label=title), count, spacing=8), count


class Dashboard(Gtk.Window):
    def __init__(self, on_refresh):
        super().__init__(title='Claude sessions')
        _cls(self, 'dash')
        self.set_default_size(540, 760)
        self.connect('delete-event', lambda w, _e: w.hide() or True)   # closing only hides
        self.connect('key-press-event', lambda w, e: e.keyval == Gdk.KEY_Escape and (w.hide() or True))
        hb = Gtk.HeaderBar(title='Claude sessions', subtitle='Live · updates automatically',
                           show_close_button=True)
        hb.pack_end(_icon_btn('view-refresh-symbolic', 'Refresh now', lambda *_: on_refresh()))
        hb.show_all()                    # titlebar isn't covered by the content's show_all()
        self.set_titlebar(hb)

        tiles = Gtk.Box(spacing=8, homogeneous=True)
        self.tiles = {}
        for key, name in (('running', 'Running'), ('busy', 'Busy'), ('today', 'Today tokens'),
                          ('cache', 'Cache reads')):
            val = _lbl('tile-val')
            tiles.pack_start(_cls(_box(val, _lbl('tile-key', label=name), vertical=True), 'tile'), True, True, 0)
            self.tiles[key] = val
        banner = _cls(_box(_lbl('kicker', label='CLAUDE CODE'), _lbl('hero', label='Your sessions'),
                           vertical=True, spacing=2), 'banner')
        banner.pack_start(tiles, False, False, 10)

        live_head, self.live_count = _section('Running')
        self.live_box = _box(vertical=True, spacing=10)
        self.empty = _lbl('empty', wrap=True, label='No running sessions. Start one by running claude in a terminal.')
        ended_head, self.ended_count = _section('Recently ended')
        self.ended_box = _box(vertical=True, spacing=10, margin_top=10)
        exp = Gtk.Expander(expanded=True)
        exp.set_label_widget(ended_head)
        exp.add(self.ended_box)
        note = _lbl('faint', wrap=True, label='Plan usage limits (5-hour / weekly) are not stored locally. '
                                              'Run /usage inside Claude Code to see them.')
        root = _cls(_box(banner, live_head, self.empty, self.live_box, exp, note,
                         vertical=True, spacing=12, margin=16), 'content')
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(root)
        self.add(sw)
        self.cards = {self.live_box: {}, self.ended_box: {}}
        self._seeded = set()             # boxes filled once; later cards slide in
        sw.show_all()

    def update(self, snap, now):
        live = snap['live']
        io, cr = snap['today']
        for key, v in (('running', str(len(live))), ('busy', str(sum(r['status'] == 'busy' for r in live))),
                       ('today', fmt_tok(io)), ('cache', fmt_tok(cr))):
            _set(self.tiles[key], v)
        _set(self.live_count, str(len(live)))
        _set(self.ended_count, str(len(snap['ended'])))
        self.empty.set_visible(not live)
        self._sync(self.live_box, live, now)
        self._sync(self.ended_box, snap['ended'], now)

    def _sync(self, box, rows, now):
        # Reuse card widgets keyed by session id; only add/remove/reorder what changed.
        cards = self.cards[box]
        want = {r['sid'] for r in rows}
        for sid in list(cards.keys() - want):
            cards.pop(sid).get_parent().destroy()
        slide = box in self._seeded
        for i, r in enumerate(rows):
            c = cards.get(r['sid'])
            if c is None:
                c = cards[r['sid']] = Card()
                rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                   transition_duration=280, reveal_child=not slide)
                rev.add(c)
                rev.show()
                box.pack_start(rev, False, False, 0)
                if slide:
                    GLib.idle_add(rev.set_reveal_child, True)
            box.reorder_child(c.get_parent(), i)
            c.set(r, now)
        self._seeded.add(box)


class App:
    def __init__(self):
        self.snap = None
        self._menu_sig = None
        self._prev = None                # pid -> row from the previous snapshot
        self._label_msg = self._label_timer = None
        self._write_icons()
        apply_theme()
        self.ind = AI.Indicator.new('claude-tray', 'claude-tray-none-0',
                                    AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(CACHE_DIR)
        self.ind.set_title('Claude sessions')
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.anim = Animator(self.ind)
        self.notifier = Notifier(lambda: self.show_dashboard())
        self.monitor = Monitor(lambda snap: GLib.idle_add(self.render, snap))
        self.win = Dashboard(lambda: self.monitor.poke(force=True))
        self._build_menu('Loading…', [])
        try:
            os.makedirs(SESS_DIR, exist_ok=True)
            self.fmon = Gio.File.new_for_path(SESS_DIR).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.fmon.connect('changed', lambda *_: self.monitor.poke())
        except (OSError, GLib.Error) as e:
            print('file monitor unavailable, polling only:', e, file=sys.stderr)
        self.monitor.start()
        GLib.timeout_add_seconds(UI_TICK_S, self._tick)

    @staticmethod
    def _write_icons():
        for name, frames in ICON_FRAMES.items():
            for i, svg in enumerate(frames):
                p = os.path.join(CACHE_DIR, f'claude-tray-{name}-{i}.svg')
                try:
                    with open(p) as f:
                        if f.read() == svg:
                            continue
                except OSError:
                    pass
                with open(p, 'w') as f:
                    f.write(svg)

    def _tick(self):
        if self.snap:
            self.render(self.snap)
        return True

    def render(self, snap):
        self.snap = snap
        now = time.time()
        live = snap['live']
        busy = sum(r['status'] == 'busy' for r in live)
        self._animate(live)
        self.anim.set_rest('busy' if busy else 'idle' if live else 'none', bool(busy))
        self._update_label()
        lines = []
        for r in live:
            left = 1 - min(r['ctx'] / r['window'], 1.0)
            lines.append(f'{STATUS.get(r["status"], ("", "", "•"))[2]} {r["name"]} — {r["status"]} · '
                         f'{short_model(r["model"])} · ctx {left:.0%} left · '
                         f'{fmt_dur(now - r["started"]) if r["started"] else "?"}')
        io, _ = snap['today']
        ended = [(f'{r["name"]} — {os.path.basename(r["cwd"]) or "~"}'
                  + (f' · {fmt_dur(now - r["last"])} ago' if r['last'] else ''), r) for r in snap['ended']]
        self._build_menu(f'{len(live)} running · {busy} busy · today {fmt_tok(io)} tokens', lines, ended)
        if self.win.get_visible():
            self.win.update(snap, now)
        return False

    def _update_label(self):
        n = len(self.snap['live']) if self.snap else 0
        self.ind.set_label(self._label_msg or (str(n) if n else ''), '99')

    def _flash_label(self, text):
        if self._label_timer:
            GLib.source_remove(self._label_timer)
        self._label_msg = text
        self._label_timer = GLib.timeout_add_seconds(LABEL_FLASH_S, self._clear_label)
        self._update_label()

    def _clear_label(self):
        self._label_msg = self._label_timer = None
        self._update_label()
        return False

    def _animate(self, live):
        # Keyed by PID: a session's id can change on /clear, its process can't.
        cur = {r['pid']: r for r in live}
        prev, self._prev = self._prev, cur
        if prev is None:                         # no events for what was already open at startup
            return
        msg = None
        for pid in cur.keys() - prev.keys():
            self.anim.play('spawn')
            msg = f'+ {cur[pid]["name"]} started'
        for pid, r in cur.items():
            old = prev.get(pid)
            if old and old['status'] == 'busy' and r['status'] != 'busy':
                self.anim.play('done')
                msg = f'✓ {r["name"]} done'
                self._notify_done(old, r)
        for pid in prev.keys() - cur.keys():
            self.anim.play('close')
            msg = f'✕ {prev[pid]["name"]} closed'
        if msg:
            self._flash_label(msg)

    def _notify_done(self, old, r):
        # statusUpdatedAt marks when each status began, so the busy span is the gap between them.
        took = (r['since'] or time.time()) - (old['since'] or time.time())
        if took < NOTIFY_MIN_S:
            return
        waiting = r['status'] != 'idle'
        title = f'{"⏳" if waiting else "✅"} {r["name"]} {"needs attention" if waiting else "finished"}'
        left = 1 - min(r['ctx'] / r['window'], 1.0)
        dur = f'{int(took // 60)}m {int(took % 60):02d}s' if 60 <= took < 600 else fmt_dur(took)
        body = (f'Took {dur} · {short_model(r["model"])} · {left:.0%} context left · '
                f'{r["cwd"].replace(HOME, "~", 1)}')
        self.notifier.notify(r['sid'], title, body, os.path.join(CACHE_DIR, 'claude-tray-done-4.svg'))

    def _build_menu(self, header, lines, ended=()):
        sig = (header, tuple(lines), tuple((t, r['sid']) for t, r in ended))
        if sig == self._menu_sig:        # skip DBus menu churn when nothing changed
            return
        self._menu_sig = sig
        menu = Gtk.Menu()

        def add(label, cb=None, sensitive=True, into=menu):
            it = Gtk.MenuItem(label=label)
            it.set_sensitive(sensitive)
            if cb:
                it.connect('activate', cb)
            into.append(it)
            return it

        add(header, sensitive=False)
        menu.append(Gtk.SeparatorMenuItem())
        for ln in lines or ['No running sessions']:
            add(ln, self.show_dashboard, bool(lines))
        if ended:
            sub = Gtk.Menu()
            for text, r in ended:
                add(text, lambda _i, r=r: launch_session(r), into=sub)
            add('Open session').set_submenu(sub)
        menu.append(Gtk.SeparatorMenuItem())
        dash = add('Open dashboard…', self.show_dashboard)
        add('Refresh', lambda *_: self.monitor.poke(force=True))
        add('Quit', lambda *_: Gtk.main_quit())
        menu.show_all()
        self.ind.set_menu(menu)
        self.ind.set_secondary_activate_target(dash)    # middle-click opens dashboard

    def show_dashboard(self, *_):
        if self.snap:
            self.win.update(self.snap, time.time())
        self.win.present()


def main():
    App()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    Gtk.main()


if __name__ == '__main__':
    main()
