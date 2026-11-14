"""MCP server for the DSCope M454: Model Context Protocol over stdio, no extra dependencies.

A thin shell over the openm454 CLI. Every tool runs one CLI command in a child process (python -m openm454 ...), so
the scope is opened and released per call (it accepts one USB client), a hang or crash never takes the server down,
and the CLI's watchdog, safety rules and error codes apply unchanged.

Results are compact summaries (frequency, Vpp, duty, rise/fall times, trigger state, decoded frames); the samples
stay in the saved .npz file, whose path is returned. Errors carry a stable code, a message and a hint:
NOT_FOUND (scope not connected), BUSY (DSWave or another openm454 process holds it), TIMEOUT_NO_TRIGGER (nothing
triggered within timeout_s), TRIGGER_OUT_OF_RANGE, INVALID_ARG, TIMEOUT_WATCHDOG, USB_ERROR ... (README).

Run:          python -m openm454.mcp_server      (or the openm454-mcp script)
Claude Code:  claude mcp add -s user openm454 openm454-mcp     (works in every shell: no "--" needed)
              DSWave for the FPGA bitstream is found by itself (registry, Program Files) or recorded once with
              `openm454 setup --dswave <folder>`; an env var would have to be given at registration instead
              (-e OPENM454_DSWAVE=<folder> -- openm454-mcp; in PowerShell the -- must be quoted: '--').
"""
import ctypes
import itertools
import json
import os
import subprocess
import sys
import threading
import time

from . import __version__
from .cli import STOP_REQUEST
from .paths import PKG_PARENT, ROOT

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
STRUCTURED_SINCE = '2025-06-18'      # structuredContent in tool results
STOP_GRACE_S = 15.0                  # after a timeout: time for a stopped stream/log to finish cleanly
JOB_START_WAIT_S = 20.0              # record_start waits this long for the first line (a log may probe ~2 s first)
SELFTEST_WALL_S = 1260.0             # above the CLI's own selftest watchdog (1200 s), so the CLI answers first
DECODE_FRAMES = 20                   # frames per decoder in a summary (all of them are in files.decode)
JOBS_DIR = os.path.join(ROOT, 'state', 'mcp_jobs')

INSTRUCTIONS = """Measure and generate real signals with the DSCope M454 oscilloscope on this PC (4 analog channels
A0-A3 up to 400 MSa/s, 8 digital D0-D7, signal generator 0-3 V). Start with `status`. `capture` takes one triggered
(or untriggered) record and returns measurements per channel (frequency, Vpp, duty, rise/fall times...) plus the
saved file; `analyze` / `plot` work on saved files. Long continuous recordings: `log_plan`, then `record_start`,
`record_status`, `record_stop`. `selftest` checks the scope itself (its signal checks need the scope's own
calibration and generator outputs cabled to its inputs: there is no internal loopback). Choose the lowest rate and fewest channels that answer the question.
Ask the user before connecting probes or the generator output to their circuit. Errors have a code: NOT_FOUND = not
connected, BUSY = another program holds the scope, TIMEOUT_NO_TRIGGER = no trigger within timeout_s."""

VERTICAL = {
    'vdiv': {'type': 'string', 'description': "volts/div at the probe tip: '1V' or per channel 'A0=500mV,A2=2V'"},
    'offset': {'type': 'string', 'description': "voltage at the vertical centre: '1.5V' or 'A1=1.5V'"},
    'probe': {'type': 'string', 'description': "probe factor: '10x' or 'A2=10x' (default 1x)"},
    'coupling': {'type': 'string', 'description': "'dc' / 'ac' or per channel 'A1=ac'"},
    'bw20': {'type': 'string', 'description': 'channels with the 20 MHz bandwidth limit, e.g. A1,A2'},
    'threshold': {'type': 'string', 'description': "digital input threshold 0..5 V (default '1.4V')"},
}
DECODE = {'type': 'array', 'items': {'type': 'string'},
          'description': "protocol decoders: 'uart:rx=D4,baud=115200', 'spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=0', "
                         "'i2c:scl=D6,sda=D7', 'can:rx=D6,bitrate=500k'"}
DETAIL = {'type': 'string', 'enum': ['summary', 'full'], 'default': 'summary',
          'description': "summary (default): key measurements; full: the CLI's whole JSON document"}
MAX_WALL = {'type': 'number', 'description': 'hard limit for the whole call in seconds (default 180); the command '
                                             'is stopped and an error returned when it is exceeded'}


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'name': 'status', 'title': 'Scope status',
     'description': 'Is the DSCope M454 connected and usable? Model, USB speed, firmware/FPGA state and the last '
                    'signal-generator setting. NOT_FOUND = not connected, BUSY = another program holds it.',
     'inputSchema': _schema({}), 'annotations': {'readOnlyHint': True}},
    {'name': 'capture', 'title': 'Capture a record',
     'description': 'One record from the scope with measurements per channel (analog: frequency, Vpp, min/max/mean, '
                    'duty, rise/fall; digital: state, frequency, duty, edges), the trigger result and the saved '
                    'files (.npz samples, .png plot). With `trigger` it waits up to `timeout_s` (default 5 s) and '
                    'returns TIMEOUT_NO_TRIGGER if nothing triggers; without one it records at once.',
     'inputSchema': _schema(dict({
         'channels': {'type': 'string', 'default': 'A0-A3,D0-D7', 'description': "e.g. 'A1', 'A0-A3,D0-D7'"},
         'timebase': {'type': 'string', 'description': "time/div, window = 10 divisions (e.g. '200us')"},
         'duration': {'type': 'string', 'description': "window length instead of timebase (e.g. '10ms')"},
         'rate': {'type': 'string', 'description': "sample rate (e.g. '10M'); else chosen from the window"},
         'samples': {'type': 'string', 'description': "samples per channel (e.g. '1M')"},
         'trigger': {'type': 'string', 'description': "SRC:EDGE[:LEVEL], e.g. 'A1:rise:1.4V', 'D0:fall', "
                                                      "pulse: 'A1:pulse>:1.4V:200us'"},
         'trig_mode': {'type': 'string', 'enum': ['normal', 'auto', 'force'],
                       'description': 'normal (default with a trigger): wait up to timeout_s; auto: free-run after '
                                      '0.3 s without a trigger; force (default without one): no trigger'},
         'timeout_s': {'type': 'number', 'default': 5, 'description': 'how long to wait for the trigger'},
         'trig_retries': {'type': 'integer', 'description': 'digital rise/fall trigger: re-arm up to N times when '
                                                            'a slow edge chattered and the capture fired on the '
                                                            'opposite edge (default 4; 0 = only check); '
                                                            'acq.trigger_check reports it'},
         'trig_pos': {'type': 'string', 'description': "trigger position in the window, e.g. '10%' (default 50%)"},
         'holdoff': {'type': 'string', 'description': "trigger holdoff, e.g. '1ms'"},
         'decode': DECODE,
         'edges': {'type': 'integer', 'description': 'also list the first N edge times per channel'},
         'window': {'type': 'string', 'description': "measure only t0:t1, e.g. '-1ms:1ms'"},
         'save': {'type': 'boolean', 'default': True, 'description': 'save the samples (.npz)'},
         'plot': {'type': 'boolean', 'default': True, 'description': 'save a .png plot'},
         'detail': DETAIL, 'max_wall_s': MAX_WALL}, **VERTICAL))},
    {'name': 'analyze', 'title': 'Analyze a saved capture',
     'description': 'Measurements (and optional protocol decoding) of a saved .npz capture or log file.',
     'inputSchema': _schema({'file': {'type': 'string', 'description': 'path of a saved .npz'},
                             'channels': {'type': 'string'}, 'window': {'type': 'string'},
                             'edges': {'type': 'integer'}, 'decode': DECODE, 'detail': DETAIL}, ['file']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'plot', 'title': 'Plot a saved capture',
     'description': 'Render a saved .npz to a .png (optionally a time window or some channels); returns its path.',
     'inputSchema': _schema({'file': {'type': 'string'}, 'channels': {'type': 'string'}, 'window': {'type': 'string'},
                             'out': {'type': 'string', 'description': 'png path (default next to the file)'}},
                            ['file'])},
    {'name': 'awg', 'title': 'Signal generator',
     'description': 'Drive the generator output (0..3 V, up to 1 MHz). action set: wave/freq/high/low (or vpp+dc), '
                    'duty, phase; dc: a steady level; off: park at 0 V; status: the last setting. The output keeps '
                    'running after the call. Ask the user before connecting it to their circuit.',
     'inputSchema': _schema({'action': {'type': 'string', 'enum': ['set', 'dc', 'off', 'status']},
                             'wave': {'type': 'string', 'enum': ['sine', 'square', 'triangle', 'noise']},
                             'freq': {'type': 'string', 'description': "e.g. '10k'"},
                             'high': {'type': 'string'}, 'low': {'type': 'string'}, 'vpp': {'type': 'string'},
                             'dc': {'type': 'string'}, 'duty': {'type': 'string'}, 'phase': {'type': 'string'},
                             'level': {'type': 'string', 'description': "dc level, e.g. '1.2V'"}}, ['action'])},
    {'name': 'log_plan', 'title': 'Plan a gap-free log',
     'description': 'Plan a gap-free loop-mode log without touching the scope: data rate, verdict, gap-free limit, '
                    'storage, and above the readout limit the comparison with stream (STREAM_BETTER / NEEDS_FORCE '
                    'errors carry the numbers).',
     'inputSchema': _schema(dict({'channels': {'type': 'string'}, 'rate': {'type': 'string'},
                                  'duration': {'type': 'string', 'description': "e.g. '60s', '2h', 'forever'"},
                                  'every': {'type': 'string'}, 'storage': {'type': 'string',
                                                                           'enum': ['ram', 'disk', 'none']},
                                  'keep_last': {'type': 'boolean'}, 'force': {'type': 'boolean'}}, **VERTICAL),
                            ['rate']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'record_start', 'title': 'Start a continuous recording',
     'description': 'Start a background recording: mode log (gap-free, one rate for all channels, kept in RAM and '
                    'saved at the end) or stream (records with short gaps, live statistics). Returns a job id; '
                    'follow it with record_status and end it with record_stop. One recording at a time; the scope '
                    'is busy meanwhile.',
     'inputSchema': _schema(dict({'mode': {'type': 'string', 'enum': ['log', 'stream']},
                                  'channels': {'type': 'string'}, 'rate': {'type': 'string'},
                                  'duration': {'type': 'string', 'description': "e.g. '60s', 'forever'"},
                                  'every': {'type': 'string', 'description': "signal time per status line, '1s'"},
                                  'storage': {'type': 'string', 'enum': ['ram', 'disk', 'none']},
                                  'keep_last': {'type': 'boolean'}, 'force': {'type': 'boolean'},
                                  'hook': {'type': 'string', 'description': 'log only: path of a Python file with '
                                           'process(block) run on every ~10 ms piece as it arrives (see the README '
                                           '"Live processing hook"); its returned dicts appear as events in '
                                           'record_status, {"stop": true} ends the recording'},
                                  'decode': DECODE}, **VERTICAL), ['mode'])},
    {'name': 'record_status', 'title': 'Recording status',
     'description': 'The latest status lines of a recording (backlog, live statistics) and its result once done.',
     'inputSchema': _schema({'job': {'type': 'string', 'description': 'job id (default: the latest)'},
                             'lines': {'type': 'integer', 'default': 3}}),
     'annotations': {'readOnlyHint': True}},
    {'name': 'record_stop', 'title': 'Stop a recording',
     'description': 'Stop a recording cleanly and return its result (files, measurements).',
     'inputSchema': _schema({'job': {'type': 'string'},
                             'wait_s': {'type': 'number', 'default': 120,
                                        'description': 'how long to wait for the final file to be written'}})},
    {'name': 'release', 'title': 'Release the scope',
     'description': 'Stop and release a scope left running or in a bad state by a crashed program.',
     'inputSchema': _schema({})},
    {'name': 'selftest', 'title': 'Self-test the scope',
     'description': 'Is the scope itself healthy? Wiring-free checks always run and only read the inputs: device '
                    'info, all 12 channels at 100 MSa/s, the 400/200 MSa/s top rates, capture latency (~5 s). The '
                    'signal checks (calibration square, V/div, triggers, thresholds, timeout) need the standard bench '
                    'fixture: the two outputs of the scope cabled to its inputs (there is no internal loopback): A0 <- '
                    'generator output; A1, A3 (1x probes) and A2 (10x probe) <- the 1 kHz calibration output (~2.8 V '
                    'square); D0, D2, D5, D7 <- the same calibration output; D1, D3, D4, D6 open. They are skipped (listed with the reason) unless state/fixture.json declares that wiring or '
                    'fixture is standard: ask the user to wire it and confirm first. Fails with SELFTEST_FAILED and '
                    'the failed checks (measured vs expected).',
     'inputSchema': _schema({
         'quick': {'type': 'boolean', 'default': True, 'description': 'fewer repetitions (false: the full run)'},
         'fixture': {'type': 'string', 'enum': ['file', 'standard'], 'default': 'file',
                     'description': "file: the wiring recorded in state/fixture.json; standard: the user confirmed "
                                    "the standard bench fixture is wired right now (this run only)"},
         'awg': {'type': 'boolean', 'default': False,
                 'description': 'also test the signal generator (needs the standard fixture): drives a 2 Vpp sine '
                                'and a 0-3 V square into A0, then turns it off. Only when the generator output goes '
                                'to A0 and nothing else'},
         'detail': DETAIL,
         'max_wall_s': {'type': 'number', 'description': "hard limit for the call in seconds (default 1260, above the "
                                                         "CLI's own 1200 s watchdog); a shorter limit returns "
                                                         "TIMEOUT_WATCHDOG while the run goes on and keeps the scope "
                                                         "busy until it ends"}})},
]
DEVICE_TOOLS = {'status', 'capture', 'awg', 'record_start', 'release', 'selftest'}


class ToolError(Exception):
    def __init__(self, code, message, hint=None, extra=None):
        super().__init__(message)
        self.doc = {'ok': False, 'error': dict({'code': code, 'message': message}, **({'hint': hint} if hint else {}))}
        self.doc.update(extra or {})


# ------------------------------------------------------------------------------------------------ CLI runner
STOPPABLE = ('watch', 'stream', 'log')            # end cleanly on a stop request
OFFLINE = ('analyze', 'plot')                     # never touch the scope: safe to kill


def _last_json(text, need_ok=False):
    for line in reversed((text or '').splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not need_ok or 'ok' in d:
                return d
    return None


def request_stop():
    os.makedirs(os.path.dirname(STOP_REQUEST), exist_ok=True)
    with open(STOP_REQUEST, 'w') as f:
        f.write('mcp\n')


def _kind(args):
    if args[0] in OFFLINE or '--plan' in args or args[:2] == ['debug', 'plan']:
        return 'offline'
    return 'stoppable' if args[0] in STOPPABLE else 'device'


def _tail(path_or_bytes, n=3):
    try:
        data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, 'rb').read()[-8192:]
    except OSError:
        return None
    lines = [l for l in data.decode('utf-8', 'replace').strip().splitlines() if l.strip()]
    return ' / '.join(lines[-n:]) or None


def _pid_alive(pid):
    if os.name == 'nt':
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Cli:
    """Runs openm454 CLI commands in child processes: the same package as this server (whatever the cwd or the
    installed copies) and the same data folder, so stop requests and state are shared."""

    def __init__(self, argv0=None):
        boot = 'import sys; sys.path.insert(0, %r); from openm454.cli import main; sys.exit(main())' % PKG_PARENT
        self.argv0 = list(argv0 or [sys.executable, '-X', 'utf8', '-c', boot])

    def _popen(self, args, stdout, stderr):
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return subprocess.Popen(self.argv0 + list(args), stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                env=dict(os.environ, OPENM454_HOME=ROOT), **kw)

    def run(self, args, max_wall):
        """-> the command's (last) JSON document, within about max_wall: past it an offline command is killed, a
        stream/log/watch is asked to stop (its result is returned), a scope command is left to the CLI's own
        watchdog (which stops the scope cleanly) and an error says so."""
        kind = _kind(args)
        p = self._popen(args, subprocess.PIPE, subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=max_wall)
        except subprocess.TimeoutExpired:
            if kind == 'offline':
                p.kill()
                p.communicate()
                raise ToolError('TIMEOUT_WATCHDOG', 'openm454 %s took longer than %.0f s and was stopped'
                                % (args[0], max_wall), 'raise max_wall_s for large files')
            if kind == 'stoppable':
                request_stop()
            try:
                out, err = p.communicate(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                raise ToolError('TIMEOUT_WATCHDOG', 'openm454 %s did not finish within %.0f s' % (args[0], max_wall),
                                'process %d keeps the scope until its own watchdog stops it (BUSY until then); '
                                'raise max_wall_s for long captures' % p.pid)
        doc = _last_json(out.decode('utf-8', 'replace'), need_ok=True)
        if doc is None:
            raise ToolError('INTERNAL', 'openm454 %s ended (exit %s) without a result' % (args[0], p.returncode),
                            _tail(err))
        return doc

    def start(self, args, out_path, err_path):
        with open(out_path, 'wb') as fo, open(err_path, 'wb') as fe:
            return self._popen(args, fo, fe)


class Job:
    """A background recording; its metadata is kept in JOBS_DIR so a restarted server can still stop it."""

    def __init__(self, job, pid, mode, args, t0, proc=None):
        self.job, self.pid, self.mode, self.args, self.t0, self.proc = job, pid, mode, args, t0, proc
        self.path = os.path.join(JOBS_DIR, job + '.jsonl')
        self.err = os.path.join(JOBS_DIR, job + '.err')

    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        return _pid_alive(self.pid) and self.final() is None

    def wait(self, timeout):
        t_end = time.time() + timeout
        while self.running():
            if time.time() >= t_end:
                return False
            time.sleep(0.1)
        return True

    def final(self):
        lines = _read_lines(self.path)
        return lines[-1] if lines and 'ok' in lines[-1] else None

    def save(self):
        with open(os.path.join(JOBS_DIR, self.job + '.json'), 'w') as f:
            json.dump({'job': self.job, 'pid': self.pid, 'mode': self.mode, 'args': self.args, 't0': self.t0}, f)

    @classmethod
    def load_all(cls):
        jobs = {}
        try:
            names = [n for n in os.listdir(JOBS_DIR) if n.endswith('.json')]
        except OSError:
            return jobs
        for n in names:
            try:
                d = json.load(open(os.path.join(JOBS_DIR, n)))
                jobs[d['job']] = cls(d['job'], d['pid'], d['mode'], d['args'], d['t0'])
            except (OSError, ValueError, KeyError):
                continue
        return jobs


# ------------------------------------------------------------------------------------------------ summaries
A_KEYS = ('freq_hz', 'vpp_v', 'vmin_v', 'vmax_v', 'vmean_v', 'duty_pct', 'rise_s', 'fall_s')
D_KEYS = ('state', 'freq_hz', 'duty_pct', 'edges', 'high_frac')
ACQ_KEYS = ('triggered', 'trigger_check', 'trigger_misfires', 'mode', 'analog_rate_hz', 'digital_rate_hz', 'rate_hz',
            'samples', 'duration_s', 'complete', 'stop_reason', 'transfers', 'transfers_wanted', 'host_mb_s',
            'backlog_max_mb', 'unread_at_stop_mb')
ACQ_SKIP = {'trigger_check': ('unchecked',), 'trigger_misfires': (0,)}       # left out of summaries when uneventful
LONG_TEXT = 400
PLAN_KEYS = ('rate_hz', 'channels', 'data_mb_s', 'verdict', 'readout_mb_s', 'gapfree_max_s', 'recommendation',
             'storage', 'max_duration_s', 'file_mb', 'ram_ring', 'interleaved', 'divider', 'warnings')


def _trim_decode(r):
    r = dict(r)
    for k in ('frames', 'error_frames'):
        items = r.get(k) or []
        if isinstance(items, list) and len(items) > DECODE_FRAMES:
            r[k] = items[:DECODE_FRAMES]
            r[k + '_listed'] = DECODE_FRAMES
    for k in ('text', 'data_hex', 'mosi_hex', 'miso_hex'):
        if isinstance(r.get(k), str) and len(r[k]) > LONG_TEXT:
            r[k] = r[k][:LONG_TEXT] + '...'
    return r


def short_plan(plan):
    return {k: plan[k] for k in PLAN_KEYS if plan.get(k) not in (None, [], {})}


WIRING_FREE_CHECKS = ('V01', 'V11', 'V12', 'V13')     # openm454/selftest.py WIRING_FREE (no numpy import here)


def selftest_error(doc, failed):
    if doc.get('device_error'):
        return doc['device_error']
    ids = [i['id'] for i in failed if i.get('gating')] or [i['id'] for i in failed]
    if any(i not in WIRING_FREE_CHECKS for i in ids):
        hint = ('see failed_checks (measured vs expected); a failed signal check usually means the wiring differs '
                'from the standard fixture')
    else:
        hint = ('a wiring-free check failed, so the scope or its USB link has a problem (not the wiring): run release '
                'and selftest again; recover if the scope hangs')
    return {'code': 'SELFTEST_FAILED', 'message': 'check(s) failed: %s' % (', '.join(ids) or 'see the items'),
            'hint': hint}


def summarize_selftest(doc):
    """A selftest result reduced to the ids that passed, the failed checks (measured vs expected) and the skipped
    ones grouped by reason, with counts that match those lists (checks not run after a device failure count as
    skipped here). A failed run gets an error (SELFTEST_FAILED, or the device error that stopped it)."""
    if not isinstance(doc.get('items'), list):
        return summarize(doc)                    # NOT_FOUND, BUSY ... before any check ran
    items = doc['items']
    passed = [i['id'] for i in items if i.get('pass')]
    failed = [i for i in items if i.get('pass') is False and not i.get('skipped')]
    by_reason = {}
    for i in items:
        if i.get('skipped'):
            reason = (i.get('measured') or {}).get('skipped', 'skipped')
            by_reason.setdefault(reason, []).append(i['id'])
    out = {'ok': bool(doc.get('ok')), 'cmd': 'selftest', 'passed': len(passed), 'failed': len(failed),
           'skipped': sum(len(v) for v in by_reason.values())}
    for k in ('seconds', 'note', 'stopped_at'):
        if doc.get(k) not in (None, ''):
            out[k] = doc[k]
    fx = doc.get('fixture') or {}
    out['fixture'] = {k: fx.get(k) if fx.get(k) in ('standard', 'unknown') else 'other' for k in ('analog', 'digital')}
    out['passed_checks'] = passed
    if failed:
        out['failed_checks'] = [{k: i.get(k) for k in ('id', 'desc', 'measured', 'expected', 'gating')}
                                for i in failed]
    if by_reason:
        out['skipped_checks'] = by_reason
    if not out['ok']:
        out['error'] = selftest_error(doc, failed)
    return out


def _queue_lowered(acq):
    """True when the engine queued fewer EP 0x86 reads than the geometry wanted (a small Linux usbfs budget); a
    missing transfers_wanted means not lowered."""
    n, want = acq.get('transfers'), acq.get('transfers_wanted')
    return isinstance(n, int) and isinstance(want, int) and n < want


def summarize(doc):
    """The CLI document reduced to what an agent reads: settings, trigger, key measurements, files, warnings (and
    the error, for a failed command: data up to a stop is kept and summarized too)."""
    out = {'ok': bool(doc.get('ok', False)), 'cmd': doc.get('cmd')}
    applied = doc.get('applied') or {}
    if applied:
        out['applied'] = {k: v for k, v in applied.items()
                          if k in ('channels', 'vdiv_v', 'offset_v', 'probe', 'threshold_v', 'rate_hz', 'mode')}
        if applied.get('trigger'):
            out['trigger'] = {k: v for k, v in applied['trigger'].items() if v is not None}
    if doc.get('acq'):
        acq = doc['acq']
        out['acq'] = {k: acq[k] for k in ACQ_KEYS if acq.get(k) is not None and acq[k] not in ACQ_SKIP.get(k, ())}
        if not _queue_lowered(acq):              # the geometry's depth (always on Windows): nothing to report
            out['acq'].pop('transfers', None)
            out['acq'].pop('transfers_wanted', None)
    for group, keys in (('analog', A_KEYS), ('digital', D_KEYS)):
        if doc.get(group):
            out[group] = {}
            for ch, m in doc[group].items():
                d = {k: m[k] for k in keys if m.get(k) is not None}
                d.update({k: True for k in ('clip_lo', 'clip_hi') if m.get(k)})
                d.update({k: m[k] for k in ('edges_rise_s', 'edges_fall_s') if m.get(k)})   # only with edges=N
                out[group][ch] = d
    if doc.get('decode'):
        out['decode'] = [_trim_decode(r) for r in doc['decode']]
    for k in ('error', 'files', 'plan', 'hook', 'device', 'awg', 'awg_last', 'persists_after_exit', 'note', 'records',
              'signal_s', 'seams_s', 'stopped_by', 'warnings', 'hints'):
        if doc.get(k) not in (None, {}, []):
            out[k] = doc[k]
    if isinstance(out.get('plan'), dict):
        out['plan'] = {k: v for k, v in out['plan'].items() if not k.startswith('_')}
    return out


# ------------------------------------------------------------------------------------------------ argument mapping
def _flag(flag, value):
    """[flag, value], or [flag=value] for a value starting with '-' (a window '-1ms:1ms', an offset '-1.5V'):
    argparse before Python 3.13 takes a separate '-...' argument for an option and refuses the command."""
    value = str(value)
    return [flag + '=' + value] if value.startswith('-') else [flag, value]


def _vertical(a):
    out = []
    for k in ('vdiv', 'offset', 'probe', 'coupling', 'bw20', 'threshold'):
        if a.get(k) not in (None, ''):
            out += _flag('--' + k, a[k])
    return out


def _opt(a, key, flag=None):
    v = a.get(key)
    return [] if v in (None, '') else _flag(flag or '--' + key.replace('_', '-'), v)


def capture_args(a):
    args = ['capture', '-c', a.get('channels') or 'A0-A3,D0-D7'] + _vertical(a)
    for k in ('timebase', 'duration', 'rate', 'samples', 'trig_pos', 'holdoff', 'window'):
        args += _opt(a, k)
    mode = a.get('trig_mode') or ('normal' if a.get('trigger') else 'force')
    if a.get('trigger'):
        args += ['--trig', a['trigger']]
    elif mode != 'force':
        raise ToolError('INVALID_ARG', 'trig_mode %s needs a trigger' % mode, "e.g. trigger 'A1:rise:1.4V'")
    timeout = float(a.get('timeout_s', 5.0))
    args += ['--trig-mode', mode, '--timeout', '%gs' % timeout]
    if a.get('trig_retries') is not None:
        args += ['--trig-retries', str(int(a['trig_retries']))]
    if a.get('edges'):
        args += ['--edges', str(int(a['edges']))]
    for spec in a.get('decode') or ():
        args += ['--decode', spec]
    if a.get('save') is False:
        args.append('--no-save')
    if a.get('plot') is False:
        args.append('--no-plot')
    return args


def analyze_args(a):
    args = ['analyze', a['file']] + _opt(a, 'window') + _opt(a, 'channels')
    if a.get('edges'):
        args += ['--edges', str(int(a['edges']))]
    for spec in a.get('decode') or ():
        args += ['--decode', spec]
    return args


def awg_args(a):
    act = a['action']
    extra = [k for k in ('wave', 'freq', 'high', 'low', 'vpp', 'dc', 'duty', 'phase', 'level') if a.get(k)
             not in (None, '')]
    if act in ('off', 'status'):
        if extra:
            raise ToolError('INVALID_ARG', 'awg %s takes no %s' % (act, ', '.join(extra)))
        return ['awg', act]
    if act == 'dc':
        if not a.get('level') or set(extra) - {'level'}:
            raise ToolError('INVALID_ARG', "awg dc takes only level, e.g. '1.2V'")
        return ['awg', 'dc', '--level', str(a['level'])]
    if a.get('level'):
        raise ToolError('INVALID_ARG', 'level is for action dc', 'awg set takes wave, freq, high/low (or vpp, dc)')
    args = ['awg', 'set']
    for k in ('wave', 'freq', 'high', 'low', 'vpp', 'dc', 'duty', 'phase'):
        args += _opt(a, k)
    return args


def record_args(a, plan=False):
    mode = a.get('mode', 'log')
    if mode == 'log' and a.get('decode'):
        raise ToolError('INVALID_ARG', 'log has no live decoder', 'decode the saved file with analyze (decode), or '
                                                                  'record with mode stream')
    if mode == 'stream' and (a.get('force') or a.get('keep_last') or a.get('storage') in ('ram', 'disk')):
        raise ToolError('INVALID_ARG', 'force, keep_last and storage ram/disk are log options',
                        'stream keeps the last record (storage none: nothing saved)')
    args = [mode, '-c', a.get('channels') or 'A0-A3,D0-D7'] + _vertical(a)
    for k in ('rate', 'duration', 'every'):
        args += _opt(a, k)
    if a.get('hook') and mode != 'log':
        raise ToolError('INVALID_ARG', 'hook is a log option', 'record with mode log')
    if a.get('hook') and not os.path.isabs(str(a['hook'])):
        raise ToolError('INVALID_ARG', 'hook needs an absolute path (the recording runs in the MCP server\'s folder, '
                        'not yours)', 'give the full path of the .py file')
    if mode == 'log':
        args += _opt(a, 'storage') + _opt(a, 'hook')
        if a.get('keep_last'):
            args.append('--keep-last')
        if a.get('force'):
            args.append('--force')
        if plan:
            args.append('--plan')
    else:
        for spec in a.get('decode') or ():
            args += ['--decode', spec]
        if a.get('storage') == 'none':
            args.append('--no-save')
    return args


def capture_wall(args):
    """Backstop time limit for a capture, from the arguments alone: above the CLI's own watchdog budget (trigger wait
    + ~1.5 x the record + 17 s, then 120 s + 1 us per sample value for the measurements), so the CLI always answers
    first. Pure arithmetic: the server loads no numpy (a DLL load while the main thread reads the stdin pipe can
    block on Windows)."""
    from .units import si, channel_list

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default
    try:
        an, dg = channel_list(opt('-c', 'A0-A3,D0-D7'))
        dur = si(opt('--duration'), 's') if opt('--duration') else (10 * si(opt('--timebase'), 's')
                                                                    if opt('--timebase') else 5e-3)
        if opt('--samples'):
            n = si(opt('--samples'))
        elif opt('--rate'):
            n = si(opt('--rate'), 'Hz') * dur
        else:
            n = 100e3
        n = min(n, 400e6) * max(1, len(an) + len(dg))
        extra = 0.0
        trig = (opt('--trig') or '').split(':')
        if trig[0].upper().startswith('D') and len(trig) > 1 and trig[1].lower() in ('rise', 'rising', 'fall', 'falling') \
                and opt('--trig-mode', 'auto') != 'force':     # misfire re-arms, as in the CLI's _budget
            extra = max(0, int(opt('--trig-retries', 4))) * (max(dur + 11.5, 2.5 * dur + 3.5) + 1.0 + 2e-7 * n)
        return 180.0 + si(opt('--timeout', '5s'), 's') + 3.0 * dur + 2e-6 * n + extra
    except Exception:                       # noqa: BLE001 - the CLI reports bad arguments itself, at once
        return 180.0


# ------------------------------------------------------------------------------------------------ server
class Server:
    def __init__(self, cli=None, out=None):
        self.cli = cli or Cli()
        self.out = out or sys.stdout.buffer
        self.wlock = threading.Lock()
        self.device = threading.Lock()          # one scope command at a time; also guards starting a recording
        self.version = PROTOCOL_VERSIONS[0]
        self.jobs = Job.load_all()               # recordings started by this or an earlier server
        self.job_ids = itertools.count(1)
        self.cancelled = set()

    # ------------------------------------------------------------ JSON-RPC plumbing
    def send(self, msg):
        data = (json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8')
        with self.wlock:
            self.out.write(data)
            self.out.flush()

    def error(self, mid, code, message):
        self.send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}})

    def serve(self, inp=None):
        lines = iter(inp.readline, b'') if inp is not None else _stdin_lines()
        try:
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    self.error(None, -32700, 'parse error')
                    continue
                for m in (msg if isinstance(msg, list) and msg else [msg]):
                    try:
                        self.dispatch(m)
                    except Exception as e:      # noqa: BLE001 - one bad message never ends the server
                        if isinstance(m, dict) and m.get('id') is not None:
                            self.error(m['id'], -32603, 'internal error: %r' % e)
        finally:
            self.shutdown()

    def dispatch(self, m):
        if not isinstance(m, dict) or not isinstance(m.get('method', ''), str):
            self.error(m.get('id') if isinstance(m, dict) else None, -32600, 'invalid request')
            return
        method, mid, params = m.get('method'), m.get('id'), m.get('params', {})
        if method is None:                       # a response to us (we send no requests): ignore
            return
        if not isinstance(params, dict):
            if mid is not None:
                self.error(mid, -32602, 'params must be an object')
            return
        if mid is None:                          # notification
            if method == 'notifications/cancelled':  # the call still finishes (bounded); its reply is dropped
                self.cancelled.add(params.get('requestId'))
            return
        if method == 'initialize':
            want = params.get('protocolVersion')
            self.version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self.reply(mid, {'protocolVersion': self.version,
                                    'capabilities': {'tools': {'listChanged': False}},
                                    'serverInfo': {'name': 'openm454', 'title': 'DSCope M454 oscilloscope',
                                                   'version': __version__},
                                    'instructions': INSTRUCTIONS})
        if method == 'ping':
            return self.reply(mid, {})
        if method == 'tools/list':
            return self.reply(mid, {'tools': TOOLS})
        if method == 'tools/call':
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                return self.error(mid, -32602, 'arguments must be an object')
            threading.Thread(target=self.call, args=(mid, params.get('name'), args), daemon=True).start()
            return
        self.error(mid, -32601, 'unknown method %s' % method)

    def reply(self, mid, result):
        self.send({'jsonrpc': '2.0', 'id': mid, 'result': result})

    def call(self, mid, name, args):
        try:
            if name not in {t['name'] for t in TOOLS}:
                self.error(mid, -32602, 'unknown tool %s' % name)
                return
            doc = self.run_tool(name, args)
            err = not doc.get('ok', False)
        except ToolError as e:
            doc, err = e.doc, True
        except Exception as e:                   # noqa: BLE001 - reported to the client, the server keeps running
            doc, err = {'ok': False, 'error': {'code': 'INTERNAL', 'message': repr(e)}}, True
        if mid in self.cancelled:
            return
        result = {'content': [{'type': 'text', 'text': self.text(doc)}], 'isError': err}
        if self.version >= STRUCTURED_SINCE:
            result['structuredContent'] = doc
        self.reply(mid, result)

    @staticmethod
    def text(doc):
        body = json.dumps(doc, indent=1)
        if doc.get('ok', False):
            return body
        e = doc.get('error') or {}
        head = '%s: %s' % (e.get('code'), e.get('message'))
        if e.get('hint'):
            head += '\nhint: %s' % e['hint']
        return head + '\n' + body

    # ------------------------------------------------------------ tools
    def running_jobs(self):
        return [j for j in list(self.jobs.values()) if j.running()]

    def _not_recording(self):
        busy = self.running_jobs()
        if busy:
            raise ToolError('BUSY', 'recording %s holds the scope' % busy[0].job,
                            'record_stop it first (or wait until it ends)')

    def run_tool(self, name, a):
        wall = a.get('max_wall_s')
        if name == 'status':
            with self.device:
                self._not_recording()
                doc = self.cli.run(['info'], 60.0)
            if doc.get('ok'):
                doc = {'ok': True, 'cmd': 'info', 'connected': True, 'device': doc.get('device'),
                       'awg_last': doc.get('awg_last')}
            return doc
        if name == 'capture':
            args = capture_args(a)
            with self.device:
                self._not_recording()
                doc = self.cli.run(args, float(wall) if wall else capture_wall(args))
            return doc if a.get('detail') == 'full' else summarize(doc)
        if name == 'analyze':
            doc = self.cli.run(analyze_args(a), float(wall or 180.0))
            return doc if a.get('detail') == 'full' else summarize(doc)
        if name == 'plot':
            return self.cli.run(['plot', a['file']] + _opt(a, 'window') + _opt(a, 'channels') + _opt(a, 'out'),
                                float(wall or 180.0))
        if name == 'awg':
            args = awg_args(a)
            with self.device:
                self._not_recording()
                return summarize(self.cli.run(args, 60.0))
        if name == 'log_plan':
            return summarize(self.cli.run(record_args(dict(a, mode='log'), plan=True), 60.0))
        if name == 'release':
            with self.device:
                self._not_recording()
                return self.cli.run(['release'], 60.0)
        if name == 'selftest':
            if a.get('fixture') not in (None, 'file', 'standard'):
                raise ToolError('INVALID_ARG', "fixture must be 'file' or 'standard'")
            args = ['selftest'] + (['--quick'] if a.get('quick', True) else []) + \
                ([] if a.get('awg') else ['--no-awg']) + (['--fixture', 'standard'] if a.get('fixture') == 'standard'
                                                          else [])
            with self.device:
                self._not_recording()
                doc = self.cli.run(args, float(wall) if wall else SELFTEST_WALL_S)
            if a.get('detail') != 'full':
                return summarize_selftest(doc)
            if not doc.get('ok') and not doc.get('error') and isinstance(doc.get('items'), list):
                doc = dict(doc, error=summarize_selftest(doc)['error'])
            return doc
        if name == 'record_start':
            return self.record_start(a)
        if name == 'record_status':
            return self.record_status(a)
        if name == 'record_stop':
            return self.record_stop(a)
        raise ToolError('INVALID_ARG', 'unknown tool %s' % name)

    def record_start(self, a):
        if a.get('mode') not in ('log', 'stream'):
            raise ToolError('INVALID_ARG', "mode must be 'log' or 'stream'")
        args = record_args(a)
        os.makedirs(JOBS_DIR, exist_ok=True)
        with self.device:                        # check and register together
            self._not_recording()
            job = '%s-%d-%d' % (time.strftime('%Y%m%d-%H%M%S'), os.getpid(), next(self.job_ids))
            j = Job(job, None, a['mode'], args, time.time())
            j.proc = self.cli.start(args, j.path, j.err)
            j.pid = j.proc.pid
            j.save()
            self.jobs[job] = j
        t_end = time.time() + JOB_START_WAIT_S
        while time.time() < t_end:              # until the run started (or was refused)
            lines = _read_lines(j.path)
            if not j.running() or (lines and (lines[-1].get('started') or 'i' in lines[-1])):
                break
            time.sleep(0.1)
        lines = _read_lines(j.path)
        if not j.running():
            final = j.final()
            if final is None or not final.get('ok', False):
                code, msg, hint = _err_parts(final, 'the recording ended at once without a result')
                plan = ((final or {}).get('error') or {}).get('plan')
                raise ToolError(code, msg, hint or _tail(j.err), extra={'plan': plan} if plan else None)
            return dict(summarize(final), job=job, running=False)
        out = {'ok': True, 'job': job, 'running': True, 'pid': j.pid, 'output': j.path.replace('\\', '/'),
               'args': args}
        for l in lines:
            if 'probe' in l:
                out['probe'] = l['probe']
            if l.get('started'):
                out['plan'] = short_plan(l.get('plan') or {})
                out['out_stem'] = l.get('out_stem')
        return out

    def _job(self, a):
        if not self.jobs:
            raise ToolError('INVALID_ARG', 'no recording is known to this server')
        job = a.get('job') or max(self.jobs, key=lambda k: self.jobs[k].t0)
        if job not in self.jobs:
            raise ToolError('INVALID_ARG', 'unknown job %s' % job, 'jobs: %s' % ', '.join(sorted(self.jobs)))
        return self.jobs[job]

    def record_status(self, a):
        j = self._job(a)
        lines = _read_lines(j.path)
        running = j.running()
        out = {'ok': True, 'job': j.job, 'running': running, 'mode': j.mode,
               'elapsed_s': round(time.time() - j.t0, 1)}
        stats = [l for l in lines if 'i' in l and 'ok' not in l]     # the live statistics lines
        if not stats and any('event' in l for l in lines):           # hook events crowd the tail: look further back
            stats = [l for l in _read_lines(j.path, 16 * 2 ** 20) if 'i' in l and 'ok' not in l]
        out['last'] = stats[-min(20, max(1, int(a.get('lines', 3)))):]
        events = [l for l in lines if 'event' in l]                  # what a log --hook reported
        if events:
            out['events'] = events[-min(20, max(1, int(a.get('lines', 3)))):]
            out['events_seen'] = len(events)
        final = j.final()
        if not running:
            out['result'] = summarize(final) if final else {
                'ok': False, 'error': {'code': 'INTERNAL', 'message': 'the recording ended without a result',
                                       'hint': _tail(j.err)}}
        return out

    def record_stop(self, a):
        if not self.jobs and not a.get('job'):  # nothing known here: stop whatever stream/log/watch runs
            request_stop()
            return {'ok': True, 'note': 'no recording is known to this server; a stop request was sent (a running '
                                        'openm454 stream, log or watch ends cleanly)'}
        j = self._job(a)
        wait = float(a.get('wait_s', 120))
        t_end = time.time() + wait
        while j.running() and time.time() < t_end:
            request_stop()                       # again each second: a run that is just starting clears old ones
            j.wait(min(1.0, max(0.0, t_end - time.time())))
        if j.running():
            raise ToolError('TIMEOUT_WATCHDOG', 'recording %s has not finished %.0f s after the stop request'
                            % (j.job, wait),
                            'a large RAM log takes a while to write: call record_stop again with a longer wait_s')
        final = j.final()
        if final is None:
            raise ToolError('INTERNAL', 'recording %s ended without a result' % j.job, _tail(j.err))
        out = dict(summarize(final), job=j.job, running=False)
        if isinstance(out.get('plan'), dict):
            out['plan'] = short_plan(out['plan'])
        return out

    def shutdown(self):
        """stdin closed (or the loop ended): a recording must not outlive the client unattended."""
        for j in self.running_jobs():
            if j.proc is None:
                continue                         # started by an earlier server: left to its owner
            request_stop()
            j.wait(120)


def _stdin_lines():
    """Lines from stdin. On Windows a pipe is polled (PeekNamedPipe) and only data already there is read: a thread
    blocked in a synchronous read of the stdin pipe would block other threads that touch the handle (DLL start-up
    code does), and the server's tool calls run in threads."""
    if os.name == 'nt':
        import _winapi
        import msvcrt
        try:
            h = msvcrt.get_osfhandle(sys.stdin.fileno())
            _winapi.PeekNamedPipe(h, 0)
        except (OSError, ValueError, AttributeError):
            h = None
        if h is not None:
            buf = b''
            while True:
                try:
                    avail = _winapi.PeekNamedPipe(h, 0)[0]
                except OSError:                 # the client closed the pipe
                    avail = -1
                if avail > 0:
                    buf += os.read(sys.stdin.fileno(), avail)
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        yield line + b'\n'
                elif avail < 0:
                    if buf:
                        yield buf
                    return
                else:
                    time.sleep(0.01)
    yield from iter(sys.stdin.buffer.readline, b'')


def _read_lines(path, tail=256 * 1024):
    out = []
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return out
    for line in data.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


def _err_parts(doc, fallback):
    e = (doc or {}).get('error') or {}
    return e.get('code', 'INTERNAL'), e.get('message', fallback), e.get('hint')


def main():
    Server().serve()


if __name__ == '__main__':
    main()
