#!/usr/bin/env python3
"""agent-tray: top-bar indicator for AI coding agents (Claude Code, ChatGPT / Codex): sessions, models, tokens, time."""
import fcntl
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import sys
import struct
import threading
import time
import wave
from datetime import datetime

HOME = os.path.expanduser('~')
CLAUDE_DIR = os.path.join(HOME, '.claude')
SESS_DIR = os.path.join(CLAUDE_DIR, 'sessions')
PROJ_DIR = os.path.join(CLAUDE_DIR, 'projects')
CODEX_DIR = os.path.join(HOME, '.codex')
CACHE_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache')), 'agent-tray')

POLL_S = 3            # fast tick: live sessions + their transcripts
SLOW_S = 30           # slow tick: rescan all projects for ended / today's files
UI_TICK_S = 30        # re-render elapsed times even when data is unchanged
IDLE_GAP_S = 300      # gaps between messages longer than this aren't "active" time
ENDED_SHOWN = 10
CTX_STD, CTX_1M = 200_000, 1_000_000
CODEX_ACTIVE_S = 1800  # a ChatGPT / Codex thread counts as open if used this recently while its app runs
CODEX_STALE_S = 600    # an unfinished Codex turn with no writes for this long is no longer "busy"
CODEX_PROC_S = 10      # how often to rescan /proc for the ChatGPT app / codex CLI
CLAUDE_BUSY_CPU = 0.015  # fallback status: a claude process above this CPU share counts as busy
CLAUDE_SUBCMDS = {'mcp', 'config', 'update', 'doctor', 'install', 'migrate-installer', 'setup-token', 'plugin'}
CLK_TCK = os.sysconf('SC_CLK_TCK')


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


def _boot_time():
    try:
        with open('/proc/stat') as f:
            return next(int(l.split()[1]) for l in f if l.startswith('btime'))
    except (OSError, StopIteration, ValueError):
        return 0


BOOT_TIME = _boot_time()


def _argv(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
    except OSError:
        return []


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
        self.codex = CodexSource(self)
        self._ptab, self._ptime = {}, 0.0
        self._cpu = {}           # fallback claude pid -> (sampled at, cpu ticks, busy, quiet samples, since)

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

    def _get(self, path, cls=Transcript):
        t = self.cache.get(path)
        if t is None:
            t = self.cache[path] = cls(path)
        t.update()
        return t

    def procs(self, now):
        # One cached /proc pass per tick, shared by the Claude fallback and Codex: pid -> (comm, ppid, ticks, start).
        if now - self._ptime < POLL_S - 0.5:
            return self._ptab
        tab = {}
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/stat', 'rb') as f:
                    st = f.read()
            except OSError:
                continue
            lp, rp = st.find(b'('), st.rfind(b')')
            rest = st[rp + 2:].split()
            try:
                tab[int(pid)] = (st[lp + 1:rp].decode(errors='replace'), int(rest[1]),
                                 int(rest[11]) + int(rest[12]), int(rest[19]))
            except (IndexError, ValueError):
                continue
        self._ptab, self._ptime = tab, now
        return tab

    def _fallback(self, known, now):
        """Claude processes without a sessions/<pid>.json (Claude Code 2.1.285+); busy/idle is estimated."""
        tab = self.procs(now)
        claudes = {pid for pid, v in tab.items() if v[0] == 'claude' and pid not in known}
        # Claude's Bash tool runs `bash -c source …/shell-snapshots/…`, a precise "tool running" signal.
        tools = {v[1] for pid, v in tab.items()
                 if v[1] in claudes and v[0] in ('bash', 'sh', 'zsh') and 'shell-snapshots' in ' '.join(_argv(pid))}
        out = []
        for pid in claudes:
            argv = _argv(pid)
            if not argv or '-p' in argv or '--print' in argv or (len(argv) > 1 and argv[1] in CLAUDE_SUBCMDS):
                continue
            _comm, _ppid, ticks, start = tab[pid]
            prev = self._cpu.get(pid)
            rate = (ticks - prev[1]) / max(now - prev[0], .5) / CLK_TCK if prev else 0
            busy, quiet, since = prev[2:] if prev else (False, 0, now)
            if rate >= CLAUDE_BUSY_CPU or pid in tools:
                quiet = 0
                if not busy:
                    busy, since = True, now
            else:
                quiet += 1
                if busy and quiet >= 2:      # two quiet samples in a row before calling it idle
                    busy, since = False, now
            self._cpu[pid] = (now, ticks, busy, quiet, since)
            try:
                cwd = os.readlink(f'/proc/{pid}/cwd')
            except OSError:
                continue
            sid = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ('--resume', '-r')), None)
            if not sid:                      # plain `claude`: its session is the newest history in that folder
                hits = glob.glob(os.path.join(PROJ_DIR, re.sub(r'[^A-Za-z0-9]', '-', cwd), '*.jsonl'))
                sid = os.path.basename(max(hits, key=os.path.getmtime))[:-6] if hits else None
            out.append({'pid': pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': (BOOT_TIME + start / CLK_TCK) * 1000,
                        'status': 'busy' if busy else 'idle', 'statusUpdatedAt': since * 1000, 'estimated': True})
        for pid in self._cpu.keys() - claudes:
            del self._cpu[pid]
        return out

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
            'agent': 'claude',
            'key': f'claude:{s.get("pid") or sid}',
            'where': 'a terminal',
            'estimated': bool(s and s.get('estimated')),
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
        live = self._live()
        live = sorted(live + self._fallback({s.get('pid') for s in live}, now), key=lambda s: s.get('startedAt') or 0)
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

        c_live, c_ended, c_keep, limits = self.codex.scan(now)
        rows += c_live
        keep |= c_keep
        ended = sorted(ended + c_ended, key=lambda r: r['last'] or 0, reverse=True)[:ENDED_SHOWN]

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
        return {'live': rows, 'ended': ended, 'today': [io, cr], 'limits': limits}


class CodexTranscript(Transcript):
    """ChatGPT desktop / Codex CLI rollout: token totals, context, turn state and plan limits, read incrementally."""
    __slots__ = ('originator', 'turn_open', 'turn_ts', 'done_ts', 'limits', 'limits_ts', '_prev')

    def __init__(self, path):
        super().__init__(path)
        self.originator = self.turn_ts = self.done_ts = self.limits = self.limits_ts = self._prev = None
        self.turn_open = False

    def _feed(self, line):
        if not (b'"token_count"' in line or b'"task_' in line or b'"turn_' in line or b'"session_meta"' in line):
            return
        try:
            d = json.loads(line)
        except ValueError:
            return
        t, p = d.get('type'), d.get('payload') or {}
        ts = _ts(d.get('timestamp'))
        if ts:
            if self.last_ts and 0 < ts - self.last_ts < IDLE_GAP_S:
                self.active += ts - self.last_ts
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = max(ts, self.last_ts or 0)
        if t == 'session_meta':
            self.cwd, self.originator = p.get('cwd'), p.get('originator')
        elif t == 'turn_context':
            self.cwd = p.get('cwd') or self.cwd
            if p.get('model'):
                self.model = p['model']
                self.models[self.model] = None
        elif t == 'event_msg':
            kind = p.get('type')
            if kind == 'task_started':
                self.turn_open, self.turn_ts = True, ts
                self.window = p.get('model_context_window') or self.window
            elif kind in ('task_complete', 'turn_aborted'):
                self.turn_open, self.done_ts = False, ts
            elif kind == 'token_count':
                self._tokens(p, ts)

    def _tokens(self, p, ts):
        info = p.get('info') or {}
        self.window = info.get('model_context_window') or self.window
        tot, last = info.get('total_token_usage'), info.get('last_token_usage')
        if tot:
            cached = tot.get('cached_input_tokens') or 0
            u4 = [max((tot.get('input_tokens') or 0) - cached, 0), tot.get('output_tokens') or 0,
                  cached, tot.get('cache_write_input_tokens') or 0]
            # Totals are cumulative, so today's share is the growth since the previous event.
            prev, self._prev, self.tok = self._prev or [0, 0, 0, 0], u4, u4
            if ts:
                b = self.day.setdefault(time.localtime(ts)[:3], [0, 0])
                b[0] += (u4[0] - prev[0]) + (u4[1] - prev[1]) + (u4[3] - prev[3])
                b[1] += u4[2] - prev[2]
        if last:
            self.ctx = last.get('total_tokens') or 0
        if p.get('rate_limits') and ts:
            self.limits, self.limits_ts = p['rate_limits'], ts


class CodexSource:
    """Threads from ~/.codex (ChatGPT desktop app and Codex CLI): list from the state DB, details from rollouts."""

    def __init__(self, mon):
        self.mon = mon
        self.threads = []
        self._db_sig = None
        self._procs = (0.0, False, frozenset(), frozenset())  # (checked at, app running, CLI cwds, open rollouts)

    @staticmethod
    def _db_path():
        dbs = glob.glob(os.path.join(CODEX_DIR, 'state_*.sqlite'))
        return max(dbs, key=lambda p: int(re.sub(r'\D', '', os.path.basename(p)) or 0)) if dbs else None

    def _load_threads(self):
        db = self._db_path()
        if not db:
            self.threads = []
            return
        sig = tuple(os.stat(p).st_mtime_ns if os.path.exists(p) else 0 for p in (db, db + '-wal'))
        if sig == self._db_sig:          # reread only when the DB changed
            return
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1)
            try:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    'SELECT id, rollout_path, cwd, title, name, model, originator, '
                    'COALESCE(updated_at_ms, updated_at * 1000) AS updated_ms, '
                    'COALESCE(created_at_ms, created_at * 1000) AS created_ms '
                    'FROM threads WHERE archived = 0 ORDER BY updated_ms DESC LIMIT ?', (ENDED_SHOWN * 2,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            print('codex db unreadable:', e, file=sys.stderr)
            return
        self.threads, self._db_sig = [dict(r) for r in rows], sig

    def _running(self, now):
        # Cached /proc scan: ChatGPT app running, interactive `codex` folders, and rollouts held open by codex.
        if now - self._procs[0] < CODEX_PROC_S:
            return self._procs[1:]
        app, cwds, held = False, set(), set()
        for pid, (comm, *_rest) in self.mon.procs(now).items():
            if comm == 'ChatGPT':
                app = True
            elif comm == 'codex':
                held.update(self._open_rollouts(pid))
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        daemon = b'app-server' in f.read()
                    if not daemon:           # the background app-server's folder says nothing about open threads
                        cwds.add(os.readlink(f'/proc/{pid}/cwd'))
                except OSError:
                    pass
        self._procs = (now, app, frozenset(cwds), frozenset(held))
        return self._procs[1:]

    @staticmethod
    def _open_rollouts(pid):
        # The codex app-server keeps an open thread's rollout file open, which is an exact "open" signal.
        out = []
        try:
            for fd in os.listdir(f'/proc/{pid}/fd'):
                try:
                    target = os.readlink(f'/proc/{pid}/fd/{fd}')
                except OSError:
                    continue
                if target.endswith('.jsonl') and '/rollout-' in target:
                    out.append(target)
        except OSError:
            pass
        return out

    def scan(self, now):
        self._load_threads()
        app, cli_cwds, held = self._running(now)
        live, ended, keep, limits = [], [], set(), None
        for th in self.threads:
            p = th.get('rollout_path')
            if not p:
                continue
            t = self.mon._get(p, CodexTranscript)
            keep.add(p)
            try:
                mtime = os.stat(p).st_mtime
            except OSError:
                mtime = (th.get('updated_ms') or 0) / 1000
            cwd = th.get('cwd') or t.cwd or ''
            in_app = 'desktop' in (th.get('originator') or t.originator or '').lower()
            recent = now - max(mtime, (th.get('updated_ms') or 0) / 1000) < CODEX_ACTIVE_S
            busy = t.turn_open and now - mtime < CODEX_STALE_S
            is_live = busy or p in held or (recent and (app if in_app else cwd in cli_cwds))
            name = (th.get('name') or th.get('title') or os.path.basename(cwd) or th['id'][:8]).strip()
            name = name if len(name) <= 40 else name[:39] + '…'
            (live if is_live else ended).append({
                'agent': 'codex', 'key': 'codex:' + th['id'], 'sid': th['id'], 'name': name,
                'title': None, 'cwd': cwd, 'pid': None, 'where': 'ChatGPT app' if in_app else 'Codex CLI', 'estimated': False,
                'status': ('busy' if busy else 'idle') if is_live else 'ended',
                'started': t.first_ts or (th.get('created_ms') or 0) / 1000 or None,
                'since': t.turn_ts if busy else (t.done_ts or t.last_ts),
                'last': t.last_ts or mtime, 'active': t.active,
                'model': t.model or th.get('model'), 'models': list(t.models), 'tok': list(t.tok),
                'ctx': t.ctx, 'window': t.window, 'cost': None, 'api_ms': None})
            if t.limits and (limits is None or t.limits_ts > limits[0]):
                limits = (t.limits_ts, t.limits)
        return live, ended, keep, limits and limits[1]


# ---------------------------------------------------------------- startup (before GTK is loaded)

def _single_instance():
    os.makedirs(CACHE_DIR, exist_ok=True)
    lock = open(os.path.join(CACHE_DIR, 'lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('agent-tray is already running (look for the icon in the top bar).')
        sys.exit(0)
    return lock


def _detach():
    # Fork into the background so closing the terminal doesn't remove the icon.
    if os.fork():
        os._exit(0)
    os.setsid()
    null = os.open(os.devnull, os.O_RDONLY)
    log = os.open(os.path.join(CACHE_DIR, 'agent-tray.log'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
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
    # Inherited from a terminal tab; gnome-terminal would try to attach new windows to that (maybe closed) tab.
    for _k in ('GNOME_TERMINAL_SCREEN', 'GNOME_TERMINAL_SERVICE'):
        os.environ.pop(_k, None)

import gi  # noqa: E402  (imported after fork so the child owns the display connection)
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Pango', '1.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import AyatanaAppIndicator3 as AI, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango  # noqa: E402

NAVY, DUSK = ('#2B4A86', '#0A1026'), ('#3B4150', '#181B22')
SPARK, SPARK_GREY, PLANET_GREEN = '#C9B6FF', '#A3A8B3', ('#7CF2A8', '#1C9A52')
CLAUDE_C, CODEX_C = '#E8835C', '#10A37F'   # per-agent colours used in the menu and dashboard
RING, RING_GREY, SAT_GLOW = '#FFDCC8', '#9CA3AF', '#6FF7E0'
AMBER, RED = ('#FCD34D', '#D97706'), ('#F87171', '#B91C1C')
ANIM_MS = {'spawn': 85, 'done': 120, 'close': 110, 'warn': 110, 'full': 110, 'orbit': 200, 'ignite': 80}
SAT_COLORS = {'claude': '#FF9F6E', 'codex': '#3DDCA8'}   # busy satellite glow per agent
NOTIFY_MIN_S = 10     # only popup for tasks that ran at least this long
SOUND = True          # play a soft chime with each popup (GNOME keeps it silent during Do Not Disturb)
# Chimes as [(start s, Hz)], volume: rising "mission complete", falling "heads-up", low "limit" nudge.
CHIMES = {'done': ([(0, 659.25), (0.11, 987.77), (0.22, 1318.5)], .32),
          'warn': ([(0, 783.99), (0.16, 587.33)], .28),
          'limit': ([(0, 523.25), (0.14, 523.25), (0.28, 392.0)], .28)}
LABEL_FLASH_S = 4     # how long an event message stays next to the icon
CTX_WARN, CTX_FULL = 0.80, 0.95   # context-used fractions that trigger a warning / "almost full" popup
CTX_REARM = 0.70      # warnings reset once usage drops below this (after /compact or /clear)
LIMIT_WARN = 0.80     # ChatGPT plan window (5-hour / weekly) usage that triggers a popup
WARN_ACTIVE_S = 3600  # context warnings only for sessions with a message in the last hour

STARS = ((5.5, 6, 1.0), (26.5, 5, .8), (4.8, 24.5, .7), (27.5, 26.5, .9), (15.5, 3.2, .6), (21.5, 29.2, .6))
ORBIT_RX, ORBIT_RY = 13, 4.2
ROCKET = ('<path d="M-2.2 1.8 L-4.4 6 L-2 5 Z M2.2 1.8 L4.4 6 L2 5 Z" fill="#E8855A"/>'
          '<path d="M0 -7.5 C3.2 -4.5 3.2 2 2.2 5 L-2.2 5 C-3.2 2 -3.2 -4.5 0 -7.5 Z" fill="#fff"/>'
          '<circle cx="0" cy="-1.8" r="1.4" fill="#5AB0FF"/>')


def _mix(a, b, t):
    pa, pb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return '#%02x%02x%02x' % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def _grad(gid, c, x2=1, y2=1):
    return (f'<linearGradient id="{gid}" x1="0" y1="0" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{c[0]}"/><stop offset="1" stop-color="{c[1]}"/></linearGradient>')


def _stars(twinkle, dim, warp):
    # twinkle=None keeps stars steady; warp stretches them into streaks during a launch.
    out = []
    for i, (x, y, r) in enumerate(STARS):
        a = (.75 if twinkle is None else .3 + .7 * ((i + twinkle) % 3 == 0)) * dim
        if warp:
            out.append(f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y + 5 * warp:.1f}" stroke="#fff" '
                       f'stroke-width="{r:.2f}" stroke-linecap="round" opacity="{a:.2f}"/>')
        else:
            out.append(f'<circle cx="{x}" cy="{y}" r="{r:.2f}" fill="#fff" opacity="{a:.2f}"/>')
    return ''.join(out)


def _satellite(theta, glow=SAT_GLOW):
    # Glowing satellite plus a fading trail, in the orbit's own (untilted) coordinates.
    out = []
    for k, (a, r) in enumerate(((.18, 1.0), (.35, 1.3), (1.0, 1.9))):
        t = theta - (2 - k) * .32
        x, y = ORBIT_RX * math.cos(t), ORBIT_RY * math.sin(t)
        if k == 2:
            out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.6" fill="{glow}" opacity=".45"/>')
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r}" fill="{glow if k < 2 else "#fff"}" '
                   f'opacity="{a:.2f}"/>')
    return ''.join(out)


def _star4(x, y, r, fill):
    k = .2 * r
    return (f'<path d="M{x} {y - r} C{x + k} {y - k} {x + k} {y - k} {x + r} {y} '
            f'C{x + k} {y + k} {x + k} {y + k} {x} {y + r} C{x - k} {y + k} {x - k} {y + k} {x - r} {y} '
            f'C{x - k} {y - k} {x - k} {y - k} {x} {y - r} Z" fill="{fill}"/>')


def _spark(color):
    # Generic "AI" mark: one large four-point star with two small white companions.
    return _star4(-1.6, 1.4, 9.2, color) + _star4(6.4, -6.2, 3.6, '#fff') + _star4(7.2, 5.6, 2.2, '#fff')


def _claude_logo(color=CLAUDE_C, k=1.0):
    rays = ''.join(f'<line x1="0" y1="{-1.6 * k:.2f}" x2="0" y2="{-ln * k:.2f}" transform="rotate({i * 36})"/>'
                   for i, ln in enumerate((7, 5.8) * 5))
    return (f'<g stroke="{color}" stroke-width="{2 * k:.2f}" stroke-linecap="round">{rays}'
            f'<circle r="{1.8 * k:.2f}" fill="{color}" stroke="none"/></g>')


def _codex_logo(color=CODEX_C, k=1.0):
    pts = ' '.join(f'{6.4 * k * math.cos(math.radians(a)):.2f},{6.4 * k * math.sin(math.radians(a)):.2f}'
                   for a in range(30, 390, 60))
    return (f'<polygon points="{pts}" fill="none" stroke="{color}" stroke-width="{2 * k:.2f}" stroke-linejoin="round"/>'
            f'<circle r="{2.4 * k:.2f}" fill="{color}"/>')


def agent_logo_svg(agent):
    """20px agent mark: orange spark for Claude Code, teal hexagon for ChatGPT / Codex."""
    body = _claude_logo() if agent == 'claude' else _codex_logo()
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="-10 -10 20 20">{body}</svg>'


def space_svg(bg=NAVY, body=SPARK, ring=RING, scale=1.0, rot=0.0, dy=0.0, fade=1.0, sats=(), check=False,
              rocket=None, flame=1.0, warp=0.0, burst=None, twinkle=None, dim=1.0, shock=None):
    """Starry badge with the AI mark in a faint orbit; extras: satellites [(angle, glow)], rocket, check planet,
    starburst, and shock=(radius, colour, opacity) for the ignition ring."""
    s = ['<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><defs>',
         _grad('bg', bg, 0.4, 1), _grad('p', PLANET_GREEN), '</defs>',
         '<rect x="0" y="0" width="32" height="32" rx="8" fill="url(#bg)"/>', _stars(twinkle, dim, warp)]
    if body:
        orbit = '<g transform="rotate(-20)">'
        back = ''.join(_satellite(a, g) for a, g in sats if math.sin(a) < 0)
        front = ''.join(_satellite(a, g) for a, g in sats if math.sin(a) >= 0)
        s.append(f'<g opacity="{fade:.2f}" transform="translate(16 {16 + dy:.2f}) scale({scale:.3f})">'
                 f'{orbit}<ellipse rx="{ORBIT_RX}" ry="{ORBIT_RY}" fill="none" stroke="{ring}" stroke-width="1.3" '
                 f'opacity=".4"/>{back}</g>')
        if check:
            s.append('<circle r="7.6" fill="url(#p)"/><circle cx="-2.4" cy="-2.6" r="2.3" fill="#fff" opacity=".28"/>'
                     '<path d="M-3.8 0.2 L-1.1 3 L4 -2.6" fill="none" stroke="#fff" stroke-width="2.4" '
                     'stroke-linecap="round" stroke-linejoin="round"/>')
        else:
            s.append(f'<g transform="rotate({rot:.1f})">{_spark(body)}</g>')
        s.append(f'{orbit}{front}</g></g>')
    if shock:
        r, color, a = shock
        s.append(f'<circle cx="16" cy="16" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="2.2" '
                 f'opacity="{a:.2f}"/><circle cx="16" cy="16" r="{r * .6:.1f}" fill="{color}" opacity="{a * .25:.2f}"/>')
    if rocket is not None:
        s.append(f'<g transform="translate(16 {rocket:.1f}) scale(1.3)">'
                 f'<path d="M-1.7 5 Q0 {5 + 6 * flame:.1f} 1.7 5 Z" fill="#FFC04D"/>'
                 f'<path d="M-0.9 5 Q0 {5 + 3.5 * flame:.1f} 0.9 5 Z" fill="#FF6B3D"/>{ROCKET}</g>')
    if burst is not None:
        # four-point stars bursting outward from the centre
        for ang in (45, 135, 225, 315):
            d, z = 9 + 6 * burst, 2.6 * (1 - burst) + .8
            x, y = 16 + d * math.cos(math.radians(ang)), 16 + d * math.sin(math.radians(ang))
            s.append(f'<path d="M{x:.1f} {y - z:.1f} L{x + z * .3:.1f} {y - z * .3:.1f} L{x + z:.1f} {y:.1f} '
                     f'L{x + z * .3:.1f} {y + z * .3:.1f} L{x:.1f} {y + z:.1f} L{x - z * .3:.1f} {y + z * .3:.1f} '
                     f'L{x - z:.1f} {y:.1f} L{x - z * .3:.1f} {y - z * .3:.1f} Z" fill="#FFF3B0" '
                     f'opacity="{1 - burst * .8:.2f}"/>')
    s.append('</svg>')
    return ''.join(s)


def alert_svg(grad, pop=1.0, scale=1.0, flash=0.0):
    """Full amber/red badge with a bold "!" for context warnings."""
    s = ['<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><defs>',
         _grad('g', grad), f'</defs><g transform="translate(16 16) scale({pop:.3f}) translate(-16 -16)">'
         '<rect x="0" y="0" width="32" height="32" rx="8" fill="url(#g)"/>']
    if flash:
        s.append(f'<rect x="0" y="0" width="32" height="32" rx="8" fill="#fff" opacity="{flash:.2f}"/>')
    s.append(f'<g stroke="#fff" stroke-width="4.4" stroke-linecap="round" '
             f'transform="translate(16 16) scale({scale:.3f}) translate(-16 -16)">'
             '<path d="M16 7.5 L16 18"/><path d="M16 24.4 L16 24.5"/></g></g></svg>')
    return ''.join(s)


def gauge_svg(agent, used):
    """16px context gauge for menu items: agent-coloured ring fills with context used, agent mark inside."""
    color = CLAUDE_C if agent == 'claude' else CODEX_C
    mark = _claude_logo(color, .42) if agent == 'claude' else _codex_logo(color, .42)
    c = 2 * math.pi * 6.6
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            '<circle cx="8" cy="8" r="6.6" fill="none" stroke="#9CA3AF" stroke-opacity=".3" stroke-width="2"/>'
            f'<circle cx="8" cy="8" r="6.6" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" '
            f'stroke-dasharray="{c * used:.2f} {c:.2f}" transform="rotate(-90 8 8)"/>'
            f'<g transform="translate(8 8)">{mark}</g></svg>')


def _orbiters(kind, theta):
    # 'both' puts the two agents' satellites on opposite sides of the orbit.
    if kind == 'both':
        return ((theta, SAT_COLORS['claude']), (theta + math.pi, SAT_COLORS['codex']))
    return ((theta, SAT_COLORS[kind]),)


def icon_frames():
    pulse = ((.55, .5, 0), (.75, .8, 0), (.95, 1.15, 0), (1, 1.05, 0), (1, 1, 0), (1, 1, .45), (1, 1, 0),
             (1, 1, .45), (1, 1, 0), (1, 1, 0), (1, 1, 0), (1, 1, 0))
    return {
        'none': [space_svg(DUSK, SPARK_GREY, RING_GREY)],
        'idle': [space_svg()],
        # busy: one satellite per busy agent orbits the mark (orange Claude, teal ChatGPT) while stars twinkle
        **{f'busy_{k}': [space_svg(sats=_orbiters(k, .6))] for k in ('claude', 'codex', 'both')},
        **{f'orbit_{k}': [space_svg(sats=_orbiters(k, .6 + 2 * math.pi * i / 12), twinkle=i // 2) for i in range(12)]
           for k in ('claude', 'codex', 'both')},
        # a session starts working: the mark flares and a shock ring in the agent's colour expands
        **{f'ignite_{k}': [space_svg(scale=sc, shock=(r, SAT_COLORS[k], a)) for sc, r, a in
                           ((1.0, 5, .9), (1.12, 8, .85), (1.2, 11, .7), (1.12, 13.5, .5), (1.05, 15, .3), (1, 16, .1))]
           for k in ('claude', 'codex')},
        # new session: rocket launches through streaking stars, then the spark spins in
        'spawn': [space_svg(body=None, rocket=y, flame=f, warp=w) for y, f, w in
                  ((33, 1, .3), (26, .75, .6), (19, 1, .9), (12, .75, 1), (5, 1, 1), (-2, .75, .8), (-9, 1, .5))]
                 + [space_svg(scale=p, rot=r, twinkle=k) for p, r, k in
                    ((.35, -80, 0), (.7, -45, 1), (1.12, -12, 2), (1, 0, 0))],
        # task finished: green "mission complete" planet with a check and a starburst
        'done': [space_svg(check=True, scale=p, burst=t, twinkle=i % 3)
                 for i, (p, t) in enumerate(((.5, None), (.8, None), (1.15, 0), (1.05, .2), (1, .4), (1, .6),
                                             (1, .8), (1, None), (1, 0), (1, .35), (1, .7), (1, None), (1, None)))],
        # session closed: the spark sinks, greys out and fades while the stars dim
        'close': [space_svg(body=_mix(SPARK, SPARK_GREY, t), ring=_mix(RING, RING_GREY, t), dy=6 * t,
                            scale=1 - .45 * t, fade=1 - .7 * t, dim=1 - .6 * t) for t in (0, .15, .3, .45, .6, .75, .9, 1)],
        **{name: [alert_svg(grad, p, s, f) for p, s, f in pulse] for name, grad in (('warn', AMBER), ('full', RED))},
    }


def svg_pixbuf(svg, size):
    ld = GdkPixbuf.PixbufLoader.new_with_type('svg')
    ld.set_size(size, size)
    ld.write(svg.encode())
    ld.close()
    return ld.get_pixbuf()


def chime_wav(notes, vol, path, rate=44100, tail=0.9):
    """Soft bell: sine plus fading overtones, 6 ms attack and exponential decay, peak-normalised to vol."""
    n = int(rate * (max(t for t, _ in notes) + tail))
    buf = [0.0] * n
    for start, f in notes:
        s0, w1, w2, w3 = int(start * rate), 2 * math.pi * f / rate, 4 * math.pi * f / rate, 6.02 * math.pi * f / rate
        for i in range(n - s0):
            t = i / rate
            buf[s0 + i] += min(1, t / .006) * math.exp(-t * 5.5) * (
                math.sin(w1 * i) + .35 * math.sin(w2 * i) * math.exp(-t * 4) + .12 * math.sin(w3 * i) * math.exp(-t * 7))
    k = vol * 32767 / (max(abs(x) for x in buf) or 1)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f'<{n}h', *(int(x * k) for x in buf)))


def sound_path(kind):
    return os.path.join(CACHE_DIR, f'agent-tray-{kind}-v1.wav')


def write_sounds():
    # Generated once; bump the -v1 suffix after changing CHIMES to regenerate.
    for kind, (notes, vol) in CHIMES.items():
        if not os.path.exists(sound_path(kind)):
            chime_wav(notes, vol, sound_path(kind))


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

    def notify(self, key, title, body, icon, sound=None):
        if not self.proxy:
            return
        hints = {'desktop-entry': GLib.Variant('s', 'agent-tray')}
        if SOUND and sound and os.path.exists(sound_path(sound)):
            hints['sound-file'] = GLib.Variant('s', sound_path(sound))
        else:
            hints['suppress-sound'] = GLib.Variant('b', True)
        args = GLib.Variant('(susssasa{sv}i)', ('Agent Tray', self.ids.get(key, 0), icon, title, body,
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
    """Plays queued icon frame sequences, then returns to the resting icon (or the busy orbit loop)."""

    def __init__(self, ind):
        self.ind = ind
        self.queue = []
        self.timer = None
        self.frames, self.i, self.loop = [], 0, False
        self.rest, self.breathe = 'none', None     # breathe: looping animation name while busy
        self._shown = None

    def _show(self, name):
        if name != self._shown:
            self._shown = name
            self.ind.set_icon_full(f'agent-tray-{name}', 'Agent sessions')

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
            anim, self.loop = self.breathe, True
        else:
            self._show(f'{self.rest}-0')
            return
        self.frames = [f'{anim}-{i}' for i in range(len(ICON_FRAMES[anim]))]
        self.i = 0
        self._step()
        self.timer = GLib.timeout_add(ANIM_MS[anim.split('_', 1)[0]], self._step)

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
.card.claude { border-left-color: #E8835C; background-color: #140E0B; }
.card.codex { border-left-color: #10A37F; background-color: #09130F; }
.card.ended { border-left-color: #3A3A3A; }
.card.ended.claude { border-left-color: #7A4633; }
.card.ended.codex { border-left-color: #0B5E4A; }
.filter { background-color: #0F0F0F; border: 1px solid #222222; border-radius: 999px; padding: 3px; }
.filter button, .filter radiobutton { background-image: none; background-color: transparent; border: none;
    box-shadow: none; border-radius: 999px; padding: 5px 14px; color: #A3A3A3; font-weight: 700; }
.filter button:hover { background-color: #1A1A1A; color: #FFFFFF; }
.filter button:checked { background-color: #F2F2F2; color: #000000; }
.filter button.claude:checked { background-color: #E8835C; color: #FFFFFF; }
.filter button.codex:checked { background-color: #10A37F; color: #FFFFFF; }
.group { font-size: 11pt; font-weight: 800; }
.group.claude { color: #F0916A; }
.group.codex { color: #2FD9A0; }
.group-note { font-size: 9pt; color: #8FD9C2; }
.name { font-size: 12pt; font-weight: 700; color: #F2F2F2; }
.muted { color: #A3A3A3; }
.faint { color: #6E6E6E; font-size: 9pt; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 8.5pt; font-weight: 700;
    background-color: #1C1C1C; color: #9A9A9A; }
.pill.busy { background-color: #0E2A19; color: #4ADE80; }
.pill.idle { background-color: #2B2210; color: #FBBF24; }
.agent { border-radius: 6px; padding: 1px 7px; font-size: 8pt; font-weight: 700; }
.agent.claude { background-color: #2A160E; color: #F0916A; }
.agent.codex { background-color: #0B2A24; color: #34D8B0; }
.limits { font-size: 9pt; color: rgba(255, 255, 255, 0.92); }
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
.card.codex button.primary { background-color: #10A37F; box-shadow: 0 2px 10px rgba(16, 163, 127, 0.35); }
.card.codex button.primary:hover { background-color: #19B98F; }
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
CODEX_BIN = shutil.which('codex') or os.path.join(HOME, '.local', 'bin', 'codex')
CHATGPT_BIN = shutil.which('chatgpt')
AGENTS = {'claude': ('Claude Code', '🟠'), 'codex': ('ChatGPT · Codex', '🟢')}


def agent_label(r):
    if r['agent'] == 'codex':
        return 'Codex CLI' if r['where'] == 'Codex CLI' else 'ChatGPT'
    return AGENTS[r['agent']][0]
TERMINAL = shutil.which('gnome-terminal') or 'x-terminal-emulator'


def resume_cmd(r, fork=False):
    sid = shlex.quote(r['sid'])
    if r['agent'] == 'codex':
        return f'{shlex.quote(CODEX_BIN)} {"fork" if fork else "resume"} {sid}'
    return f'{shlex.quote(CLAUDE_BIN)} --resume {sid}' + (' --fork-session' if fork else '')


def open_chatgpt(*_):
    if CHATGPT_BIN:
        try:
            Gio.Subprocess.new([CHATGPT_BIN], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            print('could not open ChatGPT:', e.message, file=sys.stderr)


def open_folder(r):
    if os.path.isdir(r['cwd']):
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(r['cwd']), None)


def copy_resume(r):
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(f'cd {shlex.quote(r["cwd"])} && {resume_cmd(r)}', -1)


def _window_name(minutes):
    return {300: '5-hour', 10080: 'weekly'}.get(minutes, f'{minutes // 60}-hour' if minutes else '?')


def limits_text(limits, short=False):
    # "5-hour 3% used (resets 16:29) · weekly 0% used (resets Oct 06)"
    parts = []
    for w in ((limits or {}).get('primary'), (limits or {}).get('secondary')):
        if not w:
            continue
        name = _window_name(w.get('window_minutes'))
        used = f'{w.get("used_percent", 0):.0f}%'
        if short:
            parts.append(f'{name.replace("-hour", "h")} {used}')
            continue
        at = w.get('resets_at')
        fmt = '%H:%M' if at and at - time.time() < 86400 else '%b %d'
        parts.append(f'{name} {used} used' + (f' (resets {time.strftime(fmt, time.localtime(at))})' if at else ''))
    return ' · '.join(parts)


def ctx_bar(left, n=10):
    k = round(left * n)
    return '▰' * k + '▱' * (n - k)


def launch_session(r, fork=False):
    # New terminal in the session's folder running the agent's resume/fork; the shell stays afterwards.
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
        self.logo = Gtk.Image(valign=Gtk.Align.START, margin_top=2)
        top = Gtk.Box(spacing=10)
        top.pack_start(self.logo, False, False, 0)
        top.pack_start(names, True, True, 0)
        top.pack_end(self.pill, False, False, 0)

        self.agent = _lbl('agent')
        self.chip = _lbl('chip')
        self.also = _lbl('faint', ellipsize=E.END, no_show_all=True)
        models = _box(self.agent, self.chip, self.also, spacing=8)
        self._agent = None

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
        self.gpt_btn = _icon_btn('go-jump-symbolic', 'Open the ChatGPT app', open_chatgpt)
        self.gpt_btn.set_no_show_all(True)
        actions = _box(self.open_btn, self.gpt_btn,
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
        if r['agent'] != self._agent:
            for ctx in (self.agent.get_style_context(), self.get_style_context()):
                if self._agent:
                    ctx.remove_class(self._agent)
                ctx.add_class(r['agent'])
            self._agent = r['agent']
            self.logo.set_from_pixbuf(agent_logo(r['agent']))
            self.gpt_btn.set_visible(r['agent'] == 'codex' and bool(CHATGPT_BIN))
        _set(self.agent, agent_label(r))
        _set(self.name, esc(r['name']))
        _set(self.title, esc(r['title']))
        self.title.set_visible(bool(r['title']))
        _set(self.folder, esc(r['cwd'].replace(HOME, '~', 1))
             + ((f'  ·  PID {r["pid"]}, open in {r["where"]}' if r['pid'] else f'  ·  open in {r["where"]}')
                + ('  ·  status estimated' if r.get('estimated') else '') if live else ''))
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
        if self.row:
            open_folder(self.row)

    def _copy(self, _b):
        if self.row:
            copy_resume(self.row)


_LOGOS = {}
PREFS = os.path.join(CACHE_DIR, 'prefs.json')


def _load_pref(key, default):
    try:
        with open(PREFS) as f:
            return json.load(f).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default


def _save_pref(key, value):
    try:
        with open(PREFS) as f:
            prefs = json.load(f)
    except (OSError, ValueError):
        prefs = {}
    prefs[key] = value
    try:
        with open(PREFS, 'w') as f:
            json.dump(prefs, f)
    except OSError as e:
        print('could not save prefs:', e, file=sys.stderr)


def agent_logo(agent, size=20):
    key = (agent, size)
    if key not in _LOGOS:
        _LOGOS[key] = svg_pixbuf(agent_logo_svg(agent), size)
    return _LOGOS[key]


def _section(title):
    count = _lbl('count', valign=Gtk.Align.CENTER)
    return _box(_lbl('section', label=title), count, spacing=8), count


class Dashboard(Gtk.Window):
    def __init__(self, on_refresh):
        super().__init__(title='Agent Tray')
        _cls(self, 'dash')
        self.set_default_size(540, 760)
        self.connect('delete-event', lambda w, _e: w.hide() or True)   # closing only hides
        self.connect('key-press-event', self._on_key)
        hb = Gtk.HeaderBar(title='Agent Tray', subtitle='Claude Code · ChatGPT · live',
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
        banner = _cls(_box(_lbl('kicker', label='AGENT TRAY'), _lbl('hero', label='Your AI sessions'),
                           vertical=True, spacing=2), 'banner')
        banner.pack_start(tiles, False, False, 10)

        # Segmented filter: All / Claude Code / ChatGPT; it only hides cards, nothing is re-read.
        self.filter, self._snap = _load_pref('filter', 'all'), None
        switch = _cls(Gtk.Box(halign=Gtk.Align.START), 'filter')
        self.filter_btns, group = {}, None
        for key in ('all', *AGENTS):
            b = Gtk.RadioButton.new_from_widget(group)
            group = group or b
            b.set_mode(False)            # draw as a toggle button, not a radio dot
            _cls(b, key)
            b.set_tooltip_text(f'Show {"all sessions" if key == "all" else AGENTS[key][0] + " only"} '
                               f'(key {1 + ("all", *AGENTS).index(key)})')
            b.connect('toggled', self._on_filter, key)
            switch.pack_start(b, False, False, 0)
            self.filter_btns[key] = b
        self.filter_btns.get(self.filter, self.filter_btns['all']).set_active(True)

        live_head, self.live_count = _section('Running')
        self.live_box = _box(vertical=True, spacing=14)
        self.groups = {}
        for agent, (title, _dot) in AGENTS.items():
            count = _lbl('count', valign=Gtk.Align.CENTER)
            head = _box(Gtk.Image.new_from_pixbuf(agent_logo(agent)), _lbl('group', agent, label=title), count, spacing=8)
            note = _lbl('group-note', wrap=True, no_show_all=True)
            box = _box(vertical=True, spacing=10)
            group = _box(head, note, box, vertical=True, spacing=8, no_show_all=True)
            head.show_all()
            box.show()
            self.live_box.pack_start(group, False, False, 0)
            self.groups[agent] = (group, count, note, box)
        self.limits = self.groups['codex'][2]
        self.empty = _lbl('empty', wrap=True, label='No running sessions. Start claude or codex in a terminal, '
                                                    'or open a thread in the ChatGPT app.')
        ended_head, self.ended_count = _section('Recently ended')
        self.ended_box = _box(vertical=True, spacing=10, margin_top=10)
        exp = Gtk.Expander(expanded=True)
        exp.set_label_widget(ended_head)
        exp.add(self.ended_box)
        note = _lbl('faint', wrap=True, label='Claude plan limits (5-hour / weekly) are not stored locally; '
                                              'run /usage inside Claude Code. ChatGPT limits are shown in its section.')
        root = _cls(_box(banner, switch, live_head, self.empty, self.live_box, exp, note,
                         vertical=True, spacing=12, margin=16), 'content')
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(root)
        self.add(sw)
        self.cards = {self.ended_box: {}, **{g[3]: {} for g in self.groups.values()}}
        self._seeded = set()             # boxes filled once; later cards slide in
        sw.show_all()

    def _on_key(self, _w, e):
        if e.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        keys = {Gdk.KEY_1: 'all', Gdk.KEY_2: 'claude', Gdk.KEY_3: 'codex'}
        if e.keyval in keys:
            self.filter_btns[keys[e.keyval]].set_active(True)
            return True
        return False

    def _on_filter(self, btn, key):
        if not btn.get_active() or key == self.filter:   # 'toggled' also fires on the button being released
            return
        self.filter = key
        _save_pref('filter', key)
        if self._snap:
            self.update(self._snap, time.time())

    def _shown(self, r):
        return self.filter in ('all', r['agent'])

    def update(self, snap, now):
        self._snap = snap
        live = snap['live']
        io, cr = snap['today']
        for key, v in (('running', str(len(live))), ('busy', str(sum(r['status'] == 'busy' for r in live))),
                       ('today', fmt_tok(io)), ('cache', fmt_tok(cr))):
            _set(self.tiles[key], v)
        for key, b in self.filter_btns.items():
            n = sum(key in ('all', r['agent']) for r in live)
            label = f'All  {n}' if key == 'all' else f'{AGENTS[key][1]}  {AGENTS[key][0]}  {n}'
            if b.get_label() != label:
                b.set_label(label)
        shown_live = [r for r in live if self._shown(r)]
        _set(self.live_count, str(len(shown_live)))
        _set(self.ended_count, str(sum(self._shown(r) for r in snap['ended'])))
        self.empty.set_visible(not shown_live)
        text = limits_text(snap.get('limits'))
        _set(self.limits, f'<b>Plan usage</b> · {esc(text)}' if text else '')
        self.limits.set_visible(bool(text))
        for agent, (group, count, _note, box) in self.groups.items():
            rows = [r for r in live if r['agent'] == agent]
            _set(count, str(len(rows)))
            group.set_visible(self.filter in ('all', agent) and (bool(rows) or (agent == 'codex' and bool(text))))
            self._sync(box, rows, now)
        self._sync(self.ended_box, snap['ended'], now)
        cards = self.cards[self.ended_box]
        for r in snap['ended']:
            cards[r['sid']].get_parent().set_visible(self._shown(r))

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
        # Warnings already sent survive restarts, so reopening the app never repeats them.
        self._ctx_level = dict(_load_pref('ctx_warned', {}))     # 'agent:sid' -> 0 none, 1 warn, 2 full
        self._limit_sent = set(_load_pref('limit_warned', []))  # 'slot:resets_at' popups already shown
        self._write_icons()
        if SOUND:
            write_sounds()
        apply_theme()
        self.ind = AI.Indicator.new('agent-tray', 'agent-tray-none-0',
                                    AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(CACHE_DIR)
        self.ind.set_title('Agent sessions')
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.anim = Animator(self.ind)
        self.notifier = Notifier(lambda: self.show_dashboard())
        self.monitor = Monitor(lambda snap: GLib.idle_add(self.render, snap))
        self.win = Dashboard(lambda: self.monitor.poke(force=True))
        self._gauges = {}                # (status, used/20) -> menu icon pixbuf
        self._logo = svg_pixbuf(ICON_FRAMES['idle'][0], 16)
        self._build_menu({'live': [], 'ended': [], 'today': [0, 0], 'limits': None}, time.time())
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
                p = os.path.join(CACHE_DIR, f'agent-tray-{name}-{i}.svg')
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
        busy = {r['agent'] for r in live if r['status'] == 'busy'}
        self._animate(live)
        self._check_limits(snap.get('limits'))
        kind = 'both' if len(busy) > 1 else next(iter(busy), None)
        self.anim.set_rest(f'busy_{kind}' if kind else 'idle' if live else 'none', kind and f'orbit_{kind}')
        self._update_label()
        self._build_menu(snap, now)
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
        # Claude keys on PID (its session id changes on /clear); Codex keys on the thread id.
        cur = {r['key']: r for r in live}
        prev, self._prev = self._prev, cur
        ctx_msg = self._check_context(live, animate=prev is not None)
        if prev is None:                         # no events for what was already open at startup
            return
        msg = None
        for pid in cur.keys() - prev.keys():
            self.anim.play('spawn')
            msg = f'🚀 {cur[pid]["name"]} launched'
        for pid, r in cur.items():
            old = prev.get(pid)
            if old and old['status'] != 'busy' and r['status'] == 'busy':
                self.anim.play(f'ignite_{r["agent"]}')
                msg = f'⚡ {r["name"]} working'
            if old and old['status'] == 'busy' and r['status'] != 'busy':
                self.anim.play('done')
                msg = f'✓ {r["name"]} done'
                self._notify_done(old, r)
        for pid in prev.keys() - cur.keys():
            self.anim.play('close')
            msg = f'✕ {prev[pid]["name"]} closed'
        msg = ' · '.join(m for m in (ctx_msg, msg) if m)
        if msg:
            self._flash_label(msg)

    def _check_context(self, live, animate):
        # Each level fires once per session; dropping below CTX_REARM (e.g. /compact) re-arms both.
        # Sessions nobody has used for WARN_ACTIVE_S stay quiet until they are used again.
        levels, msg, now = self._ctx_level, None, time.time()
        before = dict(levels)
        for r in live:
            key, used = f'{r["agent"]}:{r["sid"]}', min(r['ctx'] / r['window'], 1.0)
            old = levels.get(key, 0)
            if used < CTX_REARM:
                levels.pop(key, None)
                continue
            new = max(old, 2 if used >= CTX_FULL else 1 if used >= CTX_WARN else 0)
            if new > old and now - (r['last'] or 0) < WARN_ACTIVE_S:
                levels[key] = new
                self._notify_ctx(r, new, used)
                if animate:
                    self.anim.play('full' if new == 2 else 'warn')
                    msg = f'⚠ {r["name"]} ctx {used:.0%}'
        if levels != before:
            while len(levels) > 200:         # keep the saved list small
                levels.pop(next(iter(levels)))
            _save_pref('ctx_warned', levels)
        return msg

    def _check_limits(self, limits):
        # One popup per plan window per reset period, once usage passes LIMIT_WARN.
        now = time.time()
        for slot in ('primary', 'secondary'):
            w = (limits or {}).get(slot)
            if not w or (w.get('used_percent') or 0) < LIMIT_WARN * 100:
                continue
            key = f'{slot}:{w.get("resets_at")}'
            if key in self._limit_sent:
                continue
            # Forget windows that have already reset, then remember this one.
            self._limit_sent = {k for k in self._limit_sent if (int(k.split(':')[1]) if k.split(':')[1].isdigit() else 0) > now}
            self._limit_sent.add(key)
            _save_pref('limit_warned', sorted(self._limit_sent))
            name, at = _window_name(w.get('window_minutes')), w.get('resets_at')
            self.notifier.notify(f'codex:limit:{slot}', f'⚠️ ChatGPT {name} limit {w["used_percent"]:.0f}% used',
                                 f'Plan: {limits.get("plan_type") or "?"}'
                                 + (f' · resets {time.strftime("%a %H:%M", time.localtime(at))}' if at else ''),
                                 os.path.join(CACHE_DIR, 'agent-tray-warn-4.svg'), 'limit')

    def _notify_ctx(self, r, level, used):
        full = level == 2
        title = f'{"🔴" if full else "⚠️"} {r["name"]} context {"almost full" if full else f"{used:.0%} full"}'
        tip = 'run /compact or start a new session' if full else 'consider /compact soon'
        body = (f'{agent_label(r)} · {fmt_tok(r["ctx"])} of {fmt_tok(r["window"])} used · {tip} · '
                f'{r["cwd"].replace(HOME, "~", 1)}')
        icon = os.path.join(CACHE_DIR, f'agent-tray-{"full" if full else "warn"}-4.svg')
        self.notifier.notify(f'{r["sid"]}:ctx', title, body, icon, 'warn')

    def _notify_done(self, old, r):
        # statusUpdatedAt marks when each status began, so the busy span is the gap between them.
        took = (r['since'] or time.time()) - (old['since'] or time.time())
        if took < NOTIFY_MIN_S:
            return
        waiting = r['status'] != 'idle'
        title = f'{"⏳" if waiting else "✅"} {r["name"]} {"needs attention" if waiting else "finished"}'
        left = 1 - min(r['ctx'] / r['window'], 1.0)
        dur = f'{int(took // 60)}m {int(took % 60):02d}s' if 60 <= took < 600 else fmt_dur(took)
        body = (f'{agent_label(r)} · Took {dur} · {short_model(r["model"])} · {left:.0%} context left · '
                f'{r["cwd"].replace(HOME, "~", 1)}')
        self.notifier.notify(r['sid'], title, body, os.path.join(CACHE_DIR, 'agent-tray-done-4.svg'), 'done')

    def _gauge(self, key):
        g = self._gauges.get(key)
        if g is None:
            g = self._gauges[key] = svg_pixbuf(gauge_svg(key[0], key[1] / 20), 16)
        return g

    def _menu_model(self, snap, now):
        live = snap['live']
        busy = sum(r['status'] == 'busy' for r in live)
        io, cr = snap['today']
        header = (f'Agent Tray — {len(live)} running · {busy} busy', f'Today {fmt_tok(io)} tokens · {fmt_tok(cr)} cached')
        sections = []
        for agent, (title, dot) in AGENTS.items():
            rows = [r for r in live if r['agent'] == agent]
            lim = limits_text(snap.get('limits'), short=True) if agent == 'codex' else ''
            if not rows and not lim:
                continue
            items = []
            for r in rows:
                used = min(r['ctx'] / r['window'], 1.0)
                dur = fmt_dur(now - r['started']) if r['started'] else '?'
                i, o, c_r, _ = r['tok']
                icon = {'busy': '⚡', 'idle': '💤'}.get(r['status'], '•')
                items.append((r, f'{dot}  {r["name"]}    {icon} {r["status"]} · {dur}', (
                    f'🧠  {short_model(r["model"])}' + (' · 1M context' if r['window'] == CTX_1M else ''),
                    f'{ctx_bar(1 - used)}  {1 - used:.0%} context left',
                    f'⬆ {fmt_tok(i)} in  ·  ⬇ {fmt_tok(o)} out  ·  {fmt_tok(c_r)} cached',
                    f'⏱  running {dur} · active {fmt_dur(r["active"])}',
                    f'📍  open in {r["where"]}' + (' · status estimated' if r.get('estimated') else '')),
                    (agent, round(used * 20))))
            label = title.upper() + (f'  ·  {len(rows)} running' if rows else '') + (f'  ·  plan {lim} used' if lim else '')
            sections.append((agent, label, items))
        ended = [(f'{AGENTS[r["agent"]][1]}  {r["name"]} — {os.path.basename(r["cwd"]) or "~"}'
                  + (f' · {fmt_dur(now - r["last"])} ago' if r['last'] else ''), r) for r in snap['ended']]
        return header, sections, ended

    def _build_menu(self, snap, now):
        header, sections, ended = self._menu_model(snap, now)
        sig = (header, tuple((a, lb, tuple((r['sid'], t, d, g) for r, t, d, g in items)) for a, lb, items in sections),
               tuple((t, r['sid']) for t, r in ended))
        if sig == self._menu_sig:        # skip DBus menu churn when nothing changed
            return
        self._menu_sig = sig
        menu = Gtk.Menu()

        def add(label, cb=None, icon=None, into=menu):
            if icon is None:
                it = Gtk.MenuItem(label=label)
            else:
                it = Gtk.ImageMenuItem(label=label, always_show_image=True)
                it.set_image(Gtk.Image.new_from_pixbuf(icon) if isinstance(icon, GdkPixbuf.Pixbuf)
                             else Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
            if cb:
                it.connect('activate', cb)
            into.append(it)
            return it

        add(header[0], self.show_dashboard, self._logo)
        add(header[1], self.show_dashboard)
        for agent, label, items in sections:
            menu.append(Gtk.SeparatorMenuItem())
            add(label, open_chatgpt if agent == 'codex' and CHATGPT_BIN else self.show_dashboard,
                agent_logo(agent, 16))
            for r, title, details, gauge in items:
                sub = Gtk.Menu()
                for d in details:
                    add(d, self.show_dashboard, into=sub)
                sub.append(Gtk.SeparatorMenuItem())
                add('▶  Open copy in terminal', lambda _i, r=r: launch_session(r, fork=True), into=sub)
                if agent == 'codex' and CHATGPT_BIN:
                    add('💬  Open the ChatGPT app', open_chatgpt, into=sub)
                add('📁  Open folder', lambda _i, r=r: open_folder(r), into=sub)
                add('📋  Copy resume command', lambda _i, r=r: copy_resume(r), into=sub)
                add(title, icon=self._gauge(gauge)).set_submenu(sub)
        if not any(items for _, _, items in sections):
            menu.append(Gtk.SeparatorMenuItem())
            add('🌌  No running sessions', self.show_dashboard)
        menu.append(Gtk.SeparatorMenuItem())
        if ended:
            sub = Gtk.Menu()
            for text, r in ended:
                add(text, lambda _i, r=r: launch_session(r), into=sub)
            add('Resume a past session', icon='document-open-recent-symbolic').set_submenu(sub)
        dash = add('Open dashboard…', self.show_dashboard, 'view-grid-symbolic')
        add('Refresh', lambda *_: self.monitor.poke(force=True), 'view-refresh-symbolic')
        menu.append(Gtk.SeparatorMenuItem())
        add('Quit', lambda *_: Gtk.main_quit(), 'application-exit-symbolic')
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
