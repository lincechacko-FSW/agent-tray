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
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import AyatanaAppIndicator3 as AI, Gdk, Gio, GLib, Gtk  # noqa: E402

ICON_COLORS = {'none': '#9a9a9a', 'idle': '#D97757', 'busy': '#4CAF50'}
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">'
            '<g stroke="{c}" stroke-width="2.6" stroke-linecap="round">'
            '<line x1="11" y1="2.5" x2="11" y2="19.5"/><line x1="2.5" y1="11" x2="19.5" y2="11"/>'
            '<line x1="5" y1="5" x2="17" y2="17"/><line x1="17" y1="5" x2="5" y2="17"/></g></svg>')
STATUS = {'busy': ('#4CAF50', '●', '🟢'), 'idle': ('#E0A030', '●', '🟡'), 'ended': ('#888888', '○', '⚪')}
CSS = b"""
.card { background-color: alpha(@theme_fg_color, 0.06); border-radius: 10px; padding: 10px 12px; }
.dim { opacity: 0.65; }
progressbar trough, progressbar progress { min-height: 6px; border-radius: 3px; }
progressbar progress { background-color: #4CAF50; border-color: #4CAF50; }
progressbar.warn progress { background-color: #E0A030; border-color: #E0A030; }
progressbar.crit progress { background-color: #E05050; border-color: #E05050; }
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


def _label(cls=None, **kw):
    lb = Gtk.Label(xalign=0, **kw)
    lb.set_line_wrap(True)
    if cls:
        lb.get_style_context().add_class(cls)
    return lb


def _set(widget, markup):
    if widget.get_label() != markup:     # avoid needless relayout
        widget.set_markup(markup)


class Card(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.get_style_context().add_class('card')
        self.row = None
        top = Gtk.Box(spacing=8)
        self.name = _label(hexpand=True)
        self.status = Gtk.Label(xalign=1)
        top.pack_start(self.name, True, True, 0)
        top.pack_end(self.status, False, False, 0)
        self.sub = _label('dim')
        self.model = _label()
        self.bar = Gtk.ProgressBar()
        self.ctx = _label('dim')
        self.tok = _label()
        self.time = _label('dim')
        btns = Gtk.Box(spacing=6)
        self.open_btn = Gtk.Button(label='Open session')
        self.open_btn.get_style_context().add_class('suggested-action')
        self.open_btn.connect('clicked', self._resume)
        btns.pack_start(self.open_btn, False, False, 0)
        for text, tip, cb in (('Open folder', 'Open the project folder', self._open),
                              ('⋯', 'Copy resume command', self._copy)):
            b = Gtk.Button(label=text, relief=Gtk.ReliefStyle.NONE, tooltip_text=tip)
            b.connect('clicked', cb)
            btns.pack_start(b, False, False, 0)
        for w in (top, self.sub, self.model, self.bar, self.ctx, self.tok, self.time, btns):
            self.pack_start(w, False, False, 0)
        self.show_all()

    def set(self, r, now):
        self.row = r
        color, dot, _ = STATUS.get(r['status'], ('#888888', '●', ''))
        _set(self.name, f'<b>{esc(r["name"])}</b>' + (f'  <small>{esc(r["title"])}</small>' if r['title'] else ''))
        _set(self.status, f'<span foreground="{color}">{dot} {esc(r["status"])}</span>')
        live = r['status'] != 'ended'
        _set(self.sub, f'<small>{esc(r["cwd"].replace(HOME, "~", 1))}'
                       + (f' · open in a terminal (PID {r["pid"]})' if live else '') + '</small>')
        label = 'Open copy' if live else 'Open session'
        if self.open_btn.get_label() != label:
            self.open_btn.set_label(label)
            self.open_btn.set_tooltip_text(
                'Already running elsewhere: opens a forked copy with the full history in a new terminal'
                if live else 'Resume this session in a new terminal')
        cur = r['model']
        others = [short_model(m) for m in r['models'] if m != cur]
        _set(self.model, f'Model: <b>{esc(short_model(cur))}</b>'
                         + (' (1M ctx)' if r['window'] == CTX_1M else '')
                         + (f'  <small>also: {esc(", ".join(others))}</small>' if others else ''))
        frac = min(r['ctx'] / r['window'], 1.0)
        self.bar.set_fraction(frac)
        sc = self.bar.get_style_context()
        for cls, on in (('warn', 0.7 <= frac < 0.9), ('crit', frac >= 0.9)):
            (sc.add_class if on else sc.remove_class)(cls)
        _set(self.ctx, f'<small>Context: {fmt_tok(r["ctx"])} / {fmt_tok(r["window"])} used · '
                       f'<b>{1 - frac:.0%} left</b></small>')
        i, o, cr, cw = r['tok']
        _set(self.tok, f'Tokens: in {fmt_tok(i)} · out <b>{fmt_tok(o)}</b> · '
                       f'cache read {fmt_tok(cr)} · cache write {fmt_tok(cw)}')
        if r['status'] == 'ended':
            parts = [f'Active {fmt_dur(r["active"])}']
            if r['last']:
                parts.append(f'last used {fmt_dur(now - r["last"])} ago')
        else:
            parts = [f'Running {fmt_dur(now - r["started"]) if r["started"] else "?"}',
                     f'active {fmt_dur(r["active"])}']
            if r['last']:
                parts.append(f'last message {fmt_dur(now - r["last"])} ago')
        if r['api_ms']:
            parts.append(f'API {fmt_dur(r["api_ms"] / 1000)}')
        if r['cost']:
            parts.append(f'${r["cost"]:.2f}')
        _set(self.time, f'<small>{" · ".join(parts)}</small>')

    def _open(self, _b):
        if self.row and os.path.isdir(self.row['cwd']):
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(self.row['cwd']), None)

    def _resume(self, _b):
        if self.row:
            launch_session(self.row, fork=self.row['status'] != 'ended')

    def _copy(self, _b):
        if self.row:
            cmd = f'cd {shlex.quote(self.row["cwd"])} && {resume_cmd(self.row)}'
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(cmd, -1)


class Dashboard(Gtk.Window):
    def __init__(self):
        super().__init__(title='Claude sessions')
        self.set_default_size(500, 680)
        self.connect('delete-event', lambda w, _e: w.hide() or True)   # closing only hides
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        self.header = _label()
        self.live_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.empty = _label('dim', label='No running sessions')
        self.ended_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8)
        exp = Gtk.Expander(label='Recently ended')
        exp.add(self.ended_box)
        note = _label('dim', label='<small>Plan usage limits (5-hour / weekly) are not stored locally; '
                                   'run /usage inside Claude Code to see them.</small>', use_markup=True)
        for w in (self.header, self.empty, self.live_box, exp, note):
            root.pack_start(w, False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(root)
        self.add(sw)
        self.cards = {self.live_box: {}, self.ended_box: {}}
        sw.show_all()

    def update(self, snap, now):
        live = snap['live']
        busy = sum(r['status'] == 'busy' for r in live)
        io, cr = snap['today']
        _set(self.header, f'<big><b>{len(live)} running</b></big> · {busy} busy\n'
                          f'<small>Today: <b>{fmt_tok(io)}</b> tokens in/out/cache-write · '
                          f'{fmt_tok(cr)} cache reads</small>')
        self.empty.set_visible(not live)
        self._sync(self.live_box, live, now)
        self._sync(self.ended_box, snap['ended'], now)

    def _sync(self, box, rows, now):
        # Reuse card widgets keyed by session id; only add/remove/reorder what changed.
        cards = self.cards[box]
        want = {r['sid'] for r in rows}
        for sid in list(cards.keys() - want):
            cards.pop(sid).destroy()
        for i, r in enumerate(rows):
            c = cards.get(r['sid'])
            if c is None:
                c = cards[r['sid']] = Card()
                box.pack_start(c, False, False, 0)
            box.reorder_child(c, i)
            c.set(r, now)


class App:
    def __init__(self):
        self.snap = None
        self._menu_sig = None
        self._icon = None
        self._write_icons()
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.ind = AI.Indicator.new('claude-tray', 'claude-tray-none',
                                    AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(CACHE_DIR)
        self.ind.set_title('Claude sessions')
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.win = Dashboard()
        self.monitor = Monitor(lambda snap: GLib.idle_add(self.render, snap))
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
        for state, color in ICON_COLORS.items():
            p = os.path.join(CACHE_DIR, f'claude-tray-{state}.svg')
            svg = ICON_SVG.format(c=color)
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
        icon = 'busy' if busy else 'idle' if live else 'none'
        if icon != self._icon:
            self._icon = icon
            self.ind.set_icon_full(f'claude-tray-{icon}', f'Claude: {icon}')
        self.ind.set_label(str(len(live)) if live else '', '99')
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
