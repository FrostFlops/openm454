"""The declared bench: which output is wired to which input, read from tests/hardware/bench.json.

A hardware test does not carry its wiring in constants or prose: it asks the declaration for a link by name and gets
the probe attenuation, the volts/div, the offset, the signal to put on the wire and the tolerance it must be seen
within. The file is data (checked offline by tests/unit/test_bench_declaration.py); rewiring the bench is an edit
to it, and a link that is not declared is never guessed.
"""
import json
import os

import pytest

DECLARATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hardware', 'bench.json')
ANALOG = ('A0', 'A1', 'A2', 'A3')
DIGITAL = tuple('D%d' % i for i in range(8))


def load(path=None):
    with open(path or DECLARATION, encoding='utf-8') as f:
        return json.load(f)


def wiring_gate_message(who='the hardware suite'):
    """The refusal every caller prints when nobody confirmed, for this run, the wiring the declaration assumes.

    The declaration is about a physical bench that may have been rewired since it was written, so it is an assumption,
    not a fact: pytest asks with --bench-confirmed (tests/hardware/conftest.py), and the research scripts drive the
    generator through the same gate, because a script that programs a signal into an undeclared input is the failure
    mode this guard exists for.
    """
    links = load().get('links') or {}
    lines = ['%s: %s' % (k, v.get('summary', '')) for k, v in sorted(links.items())] or ['(no links declared)']
    return ('%s assumes the bench is wired as tests/hardware/bench.json declares:\n  %s\n'
            'ask the bench owner whether it is wired exactly like that right now, and only then rerun with '
            '--bench-confirmed (never assume the wiring was kept since the last run)' % (who, '\n  '.join(lines)))


class Link:
    """One declared connection: a source driving a sink, and what a capture of that sink has to look like."""

    def __init__(self, name, d):
        self.name, self.d = name, d

    def __getitem__(self, key):
        return self.d[key]

    @property
    def channel(self):
        return self.d['sink']

    def settings(self, duration, rate):
        """A forced, untriggered capture of the sink, configured the way the declaration says it is wired."""
        from openm454 import scope as scopemod
        st = scopemod.Settings()
        if self.channel in ANALOG:
            st.analog, st.digital = [self.channel], []
        else:
            st.analog, st.digital = [], [self.channel]
        st.rate, st.duration = rate, duration
        st.probe[self.channel] = self.d['probe']
        st.vdiv[self.channel] = self.d['vdiv_v']
        st.offset[self.channel] = self.d['offset_v']
        st.trig_source, st.trig_mode = None, 'force'
        return st

    def program(self, scope):
        """Put the declared signal on the wire (an `awg` source only: nothing else is programmable from a test)."""
        if self.d['source'] != 'awg':
            raise AssertionError('%s: source %r is not programmable from a test' % (self.name, self.d['source']))
        sig = self.d['signal']
        return scope.awg_set(sig['wave'], sig['freq_hz'], sig['high_v'], sig['low_v'], sig.get('duty_pct', 50.0))

    def assert_shows_the_declared_signal(self, measure):
        """The declaration is the oracle. A mismatched capture means the bench is not the declared one (a 10x probe
        in line, a divider, another channel), so the failure names bench.json instead of a bare number."""
        sig, tol, ch = self.d['signal'], self.d['tolerance'], self.channel
        freq = measure.get('freq_hz')
        assert freq is not None and abs(freq - sig['freq_hz']) <= tol['freq_rel'] * sig['freq_hz'], \
            ('bench.json %s: declares %.6g Hz on %s, the capture measured %s Hz. Check the wiring and the '
             'probe/divider this link declares, or fix the declaration.' % (self.name, sig['freq_hz'], ch, freq))
        for key, want in (('vmax_v', sig['high_v']), ('vmin_v', sig['low_v'])):
            got = measure.get(key)
            assert got is not None and abs(got - want) <= tol['level_v'], \
                ('bench.json %s: declares %s = %.3g V on %s, the capture measured %s. Check the probe attenuation '
                 'and any divider this link declares, or fix the declaration.' % (self.name, key, want, ch, got))
        duty = measure.get('duty_pct')
        if duty is not None and 'duty_pct' in sig:
            assert abs(duty - sig['duty_pct']) <= tol['duty_abs_pct'], \
                'bench.json %s: declares %.3g %% duty on %s, the capture measured %.3g %%' % (
                    self.name, sig['duty_pct'], ch, duty)


class Bench:
    """Every link the declaration names, plus the scope it was written for."""

    def __init__(self, d):
        self.d = d
        self.links = {name: Link(name, body) for name, body in d.get('links', {}).items()}

    @property
    def declared(self):
        return sorted(self.links)

    def link(self, name):
        if name not in self.links:
            pytest.skip('bench.json declares no link %r (declared: %s)' % (name, ', '.join(self.declared) or 'none'))
        return self.links[name]


def restore_awg(scope, before):
    """Put the generator back the way the suite found it; unknown state means park it, never leave a signal on.

    The generator is write-only, so state/awg.json is the only record of what was running, and it distinguishes the
    five modes. A failure here is a bench-safety failure: the caller turns it into an error, not something to swallow.
    """
    if not before or not before.get('enabled'):
        scope.awg_off()
        return 'parked at 0 V'
    # the device quantizes levels: program what was asked for, not the quantized reading, or every restore drifts
    high = before.get('high_req_v', before.get('high_v'))
    low = before.get('low_req_v', before.get('low_v'))
    mode = before.get('mode')
    if mode == 'burst':
        scope.awg_burst(before['wave'], before['freq_hz'], high, low, before['cycles'],
                        before['period_s'], duty=before.get('duty_pct', 50.0), phase=before.get('phase_deg', 0.0))
    elif mode == 'sweep':
        scope.awg_sweep(before['start_hz'], before['end_hz'], before['time_s'],
                        sweep_type=before.get('sweep_type', 'linear'), wave=before['wave'],
                        high=high, low=low, duty=before.get('duty_pct', 50.0))
    elif mode == 'modulation':
        # the state file is flat for the modulation kind: depth_pct / deviation_hz / hop_hz sit next to 'kind'
        scope.awg_modulate(before['kind'], before['wave'], before['freq_hz'], high, low,
                           mod_wave=before.get('mod_wave', 'sine'), mod_freq=before.get('mod_freq_hz', 100.0),
                           depth=before.get('depth_pct', 50.0), deviation=before.get('deviation_hz'),
                           hop=before.get('hop_hz'), duty=before.get('duty_pct', 50.0))
    elif before.get('wave') == 'dc':
        scope.awg_dc(before.get('level_req_v', before['level_v']))
    else:
        scope.awg_set(before['wave'], before['freq_hz'], high, low,
                      before.get('duty_pct', 50.0), before.get('phase_deg', 0.0))
    return 'restored (%s)' % (mode or 'continuous')


def restore_or_park(scope, before):
    """Hand the generator back the way it was found; park it at 0 V when that fails.

    The hardware tests do this in their session fixture; a research script that drives the generator has to do it too,
    otherwise the bench is left with whatever level the experiment used. Returns True when the state came back.
    """
    try:
        restore_awg(scope, before)
        return True
    except Exception as e:                                   # noqa: BLE001 - reported after parking the output
        print('AWG restore failed (%r): parking the output at 0 V' % (e,), file=sys.stderr)
        try:
            scope.awg_off()
        except Exception:                                    # noqa: BLE001
            pass
        return False


def write_manifest(out_dir, scope, script, args, extra=None, files=(), candidates=()):
    """Where and how the numbers were taken: device, host, script and bench declaration, plus a session id.

    files: the names this run writes. candidates: the names the script may write, so one that exists in out_dir but was
    not written now (a --quick run leaves an older trace behind) is recorded as stale instead of passing for this
    run's output — a manifest that cannot tell the two apart makes the directory unreadable a week later.
    The CSVs of a research run are only comparable when the unit, the script and the declaration are named, so every
    run writes one manifest.json next to them.
    """
    import hashlib
    import json
    import platform
    import subprocess
    import time

    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # <repo>/tests/support

    def sha(path):
        try:
            with open(path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            return None

    try:
        dev = scope.info()['device']
    except Exception:                                        # noqa: BLE001 - a manifest never fails the run
        dev = {}
    try:
        git = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True,
                            text=True).stdout.strip()
    except Exception:                                        # noqa: BLE001
        git = None
    doc = {'session': time.strftime('%Y%m%d-%H%M%S'), 'date': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
           'script': os.path.basename(script), 'script_sha256': sha(script),
           'bench_json_sha256': sha(DECLARATION), 'git': git or None,
           'kernel': platform.release(), 'platform': platform.platform(), 'python': platform.python_version(),
           'device': {k: dev.get(k) for k in ('model', 'fw_version', 'panel_version', 'fpga_bin_version',
                                              'usb_speed')},
           'args': {k: v for k, v in vars(args).items() if k != 'out_dir'}, 'extra': extra or {},
           'files': [os.path.basename(str(f)) for f in files],
           'stale': sorted(n for n in candidates
                           if n not in [os.path.basename(str(f)) for f in files]
                           and os.path.exists(os.path.join(out_dir, n)))}
    os.makedirs(out_dir, exist_ok=True)
    name = 'manifest-%s.json' % os.path.splitext(os.path.basename(script))[0]
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    return doc