"""High-level API: Scope().capture(Settings) -> Capture.

User-level conventions (also used by the CLI):
  * vdiv / offset / trigger level are in volts at the probe tip; probe factor (1x/10x) divides them down
    to the BNC input, where the hardware range is 1 mV/div .. 5 V/div (8 vertical divisions, 235 ADC codes).
  * offset = the voltage displayed at the vertical centre (DSWave 'Offset' code = position of 0 V).
  * the window is `duration` long with at most `samples` per channel (or `rate` x `duration`). Available rates are
    fold x {1, 2, 5} x 10^n with fold 4 / 2 / 1 for 1 / 2 / 3-4 analog channels (digital: 1-2 / 3-4 / 5-8), up to
    400 MSa/s; the highest one that fits is used and the sample count follows it (e.g. 1 channel, 5 ms, 500k ->
    80 MSa/s, 400k samples).
  * auto trigger = wait `auto_after` for a real trigger, then capture untriggered (like DSView's auto).
"""
import json
import math
import os
import time
from dataclasses import dataclass, field, asdict

import numpy as np

from . import mso, trigcheck
from .errors import DscopeError
from .capture import Capture, CODES_PER_DIV, CODE_MIN, CODE_MAX

from .paths import ROOT
STATE = os.path.join(ROOT, 'state')
VDIVS_UV = [1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000, 5000000]
RATES = [1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000, 500000, 1000000, 2000000, 5000000,
         10000000, 20000000, 50000000, 100000000]
EDGES = {'rise': 0, 'rising': 0, 'fall': 1, 'falling': 1, 'any': 2, 'both': 2,
         'pulse>': 3, 'pulse<': 4, 'npulse>': 5, 'npulse<': 6}
#: after a run without analog channels (or at the start of a session) the analog inputs start 8-14 codes high and
#: settle within ~80 ms (measured 2026-10-31), so the first analog run waits this long after its setup writes
ANALOG_WAKE_SETTLE = 0.15
OVERDRIVE_SETTLE = 0.3           # measured: 1500 div of overdrive at 1 mV/div needs ~215 ms to settle within 0.5 LSB
MAX_SAMPLES = 400_000_000     # per channel; the 4 Gbit sample memory is the real bound (checked by mso)
EP_MAX_PACKET = 1024          # EP 0x86 wMaxPacketSize; the USB 2 (high-speed) profile's 512 divides it


@dataclass
class Settings:
    analog: list = field(default_factory=lambda: ['A0', 'A1', 'A2', 'A3'])
    digital: list = field(default_factory=lambda: ['D%d' % i for i in range(8)])
    vdiv: dict = field(default_factory=lambda: {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')})
    offset: dict = field(default_factory=lambda: {c: 0.0 for c in ('A0', 'A1', 'A2', 'A3')})
    coupling: dict = field(default_factory=lambda: {c: 'dc' for c in ('A0', 'A1', 'A2', 'A3')})
    bw20: list = field(default_factory=list)
    invert: list = field(default_factory=list)
    probe: dict = field(default_factory=lambda: {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')})
    threshold: float = 1.4
    duration: float = 5e-3
    samples: int = 100_000
    rate: float = None
    trig_source: str = None           # 'A1' / 'D0' / None
    trig_edge: str = 'rise'
    trig_level: float = None          # volts (analog); None = mid of the visible range
    trig_pulse: float = 0.0           # seconds (pulse triggers)
    trig_mode: str = 'auto'           # auto | normal | force
    trig_pos: float = 50.0            # percent of window before the trigger
    auto_after: float = 0.3
    timeout: float = 5.0
    holdoff: float = 0.0
    sensitivity: int = 5
    trig_retries: int = 4             # digital rise/fall trigger: re-arms after a misfire on the other edge


def settings_from_args(a):
    st = vertical_from_args(a)
    return _timing_from_args(st, a)


MAX_RECORD_SAMPLES = 400_000_000


def stream_record_plan(st, total, min_rate):
    """Records of a `stream` with untriggered settings st over `total` seconds (None = forever) -> (settings of
    one record, seconds per record, chained). One record holds at most 400M samples per channel and 512 MiB of
    sample memory; a longer stream is cut into records of the same rate (the largest that fits)."""
    import copy
    first_error = None
    if total is not None:
        try:
            pl = Scope().plan(st)
            if not any('samples capped' in w for w in pl['warnings']):
                return st, total, False
        except DscopeError as e:            # e.g. a long window squeezed into one record needs small packets
            if e.code != 'INVALID_ARG':
                raise
            first_error = e
    rate = st.rate or (st.samples / total if total else min_rate)   # --samples keeps its density

    def record(d):
        r = copy.copy(st)
        r.duration, r.rate = d, rate
        return r
    d = MAX_RECORD_SAMPLES / rate
    for _ in range(60):
        try:
            pl = Scope().plan(record(d))
            if not any('samples capped' in w for w in pl['warnings']):
                break
        except DscopeError as e:
            if e.code != 'INVALID_ARG':
                raise
        d *= 0.9
    else:
        raise first_error or DscopeError('INVALID_ARG', 'no record length fits these stream settings')
    if total is not None and total <= d:
        return record(total), total, False
    return record(d), d, True


def vertical_flags(st):
    """CLI options that reproduce st's channels' vertical settings and threshold (defaults left out)."""
    def spec(d, fmt, default):
        items = ['%s=%s' % (c, fmt(d[c])) for c in st.analog if c in d and d[c] != default]
        return ','.join(items)
    out = []
    for flag, d, fmt, default in (('--vdiv', st.vdiv, lambda v: '%gV' % v, 1.0),
                                  ('--offset', st.offset, lambda v: '%gV' % v, 0.0),
                                  ('--probe', st.probe, lambda v: '%gx' % v, 1.0),
                                  ('--coupling', st.coupling, str, 'dc')):
        v = spec(d, fmt, default)
        if v:
            out.append('%s %s' % (flag, v))
    for flag, chans in (('--bw20', st.bw20), ('--invert', st.invert)):
        chans = [c for c in chans if c in st.analog]
        if chans:
            out.append('%s %s' % (flag, ','.join(chans)))
    if st.digital and abs(st.threshold - 1.4) > 1e-9:
        out.append('--threshold %gV' % st.threshold)
    return ' '.join(out)


def vertical_from_args(a):
    """Channels, vertical settings and the digital threshold (shared by capture/watch/stream and log)."""
    from .units import si, channel_list, per_channel
    st = Settings()
    an, dg = channel_list(a.channels)
    st.analog, st.digital = an, dg
    st.vdiv = per_channel(a.vdiv, 'V', '--vdiv', 1.0)
    st.offset = per_channel(a.offset, 'V', '--offset', 0.0)
    st.coupling = {k: v.lower() for k, v in per_channel(a.coupling, None, '--coupling', 'dc').items()}
    for k, v in st.coupling.items():
        if v not in ('dc', 'ac'):
            raise DscopeError('INVALID_ARG', '--coupling must be dc or ac')
    st.bw20 = channel_list(a.bw20)[0] if a.bw20 else []
    st.invert = channel_list(a.invert)[0] if a.invert else []
    st.probe = per_channel(a.probe, None, '--probe', '1x')
    st.probe = {k: si(v, None, '--probe') for k, v in st.probe.items()}
    st.threshold = si(a.threshold, 'V', '--threshold')
    return st


def _timing_from_args(st, a):
    from .units import si
    if a.duration:
        st.duration = si(a.duration, 's', '--duration')
    elif a.timebase:
        st.duration = 10 * si(a.timebase, 's', '--timebase')
    if a.rate:
        st.rate = si(a.rate, 'Hz', '--rate')
    if a.samples:
        st.samples = int(si(a.samples, None, '--samples'))
    if a.trig:
        parts = a.trig.split(':')
        st.trig_source = parts[0].upper()
        if len(parts) > 1 and parts[1]:
            st.trig_edge = parts[1].lower()
        if len(parts) == 3 and st.trig_source.startswith('D') and parts[2].strip().lower().endswith('s'):
            parts = parts[:2] + ['', parts[2]]          # digital lines have no level: D4:npulse>:200us
        if len(parts) > 2 and parts[2]:
            st.trig_level = si(parts[2], 'V', 'trigger level')
        if len(parts) > 3 and parts[3]:
            st.trig_pulse = si(parts[3], 's', 'pulse width')
    st.trig_mode = a.trig_mode
    st.trig_pos = si(a.trig_pos, None, '--trig-pos')
    st.auto_after = si(a.auto_after, 's', '--auto-after')
    st.timeout = si(a.timeout, 's', '--timeout')
    st.holdoff = si(a.holdoff, 's', '--holdoff')
    st.sensitivity = a.sensitivity
    r = getattr(a, 'trig_retries', None)
    if r is not None:
        st.trig_retries = int(si(r, None, '--trig-retries'))
        if st.trig_retries < 0:
            raise DscopeError('INVALID_ARG', '--trig-retries must be >= 0')
    return st


# ------------------------------------------------------------------------------------------------ HAL config
def _fold(n, table):
    for lim, f in table:
        if n <= lim:
            return f
    return 1


def afold_for(n):
    return 0 if n == 0 else _fold(n, [(1, 4), (2, 2), (4, 1)])


def dfold_for(n):
    return 0 if n == 0 else _fold(n, [(2, 4), (4, 2), (8, 1)])


def hal_config(st):
    """User Settings -> SCPI-level HAL parameters (+ per-channel scaling) -> dict."""
    warnings, hints = [], []
    for c in st.analog + st.digital:
        pass
    if not st.analog and not st.digital:
        raise DscopeError('INVALID_ARG', 'no channels enabled')
    if not (st.duration and st.duration > 0 and st.duration * 1e12 >= 1):
        raise DscopeError('INVALID_ARG', '--duration/--timebase must be > 0')
    if st.trig_source and st.trig_source not in st.analog + st.digital:
        raise DscopeError('INVALID_ARG', 'trigger source %s is not an enabled channel' % st.trig_source)
    if st.trig_edge not in EDGES:
        raise DscopeError('INVALID_ARG', 'edge must be one of %s' % ', '.join(EDGES))
    ana = []
    scale = {}
    for i in range(4):
        ch = 'A%d' % i
        probe = float(st.probe.get(ch) or 1.0)
        want = st.vdiv.get(ch, 1.0) / probe
        hw = next((v for v in VDIVS_UV if v >= want * 1e6 * 0.999), VDIVS_UV[-1])
        if ch in st.analog and hw / 1e6 < want * 0.999:
            warnings.append('%s: %.3g V/div exceeds the 5 V/div range at %gx probe' % (ch, want * probe, probe))
        vdiv_v = hw / 1e6
        vpc = vdiv_v / CODES_PER_DIV
        zero = int(round(128 - (st.offset.get(ch, 0.0) / probe) / vpc))
        if not 0 <= zero <= 255 and ch in st.analog:
            lo, hi = -127 * vpc * probe, 128 * vpc * probe          # the offset DAC: about -4.32 .. +4.36 divisions
            used = (128 - min(max(zero, 0), 255)) * vpc * probe
            warnings.append('%s: offset %.3g V out of range at %g V/div (allowed %.4g .. %.4g V, about +-4.3 div); '
                            'clamped to %.4g V' % (ch, st.offset.get(ch, 0.0), vdiv_v * probe, lo, hi, used))
        zero = min(max(zero, 0), 255)
        lvl = None
        if st.trig_source == ch:
            lv = st.trig_level
            if lv is None:
                lv = st.offset.get(ch, 0.0)
            code = int(round(zero + (lv / probe) / vpc))
            if not CODE_MIN <= code <= CODE_MAX and st.trig_mode == 'force':
                code = min(max(code, CODE_MIN), CODE_MAX)          # force mode never uses the trigger: no refusal
            if not CODE_MIN <= code <= CODE_MAX:
                lo, hi = ((c - zero) * vpc * probe for c in (CODE_MIN, CODE_MAX))
                raise DscopeError('TRIGGER_OUT_OF_RANGE', 'trigger level %.4g V is outside the visible range of %s: '
                                  '%.4g..%.4g V at %g V/div, offset %g V' % (lv, ch, lo, hi, vdiv_v * probe,
                                                                             st.offset.get(ch, 0.0)),
                                  'set --offset near the level, or a larger --vdiv (the screen is 8 divisions)')
            lvl = code
        ana.append({'enable': int(ch in st.analog), 'coupling': st.coupling.get(ch, 'dc'), 'bw20': int(ch in st.bw20),
                    'invert': int(ch in st.invert), 'vdiv_uv': hw, 'voff_code': zero, 'trig_level_code': lvl if lvl is not None else zero})
        scale[ch] = {'zero_code': zero, 'vdiv_v': vdiv_v, 'volts_per_code': vpc, 'probe': probe}
    n_a, n_d = len(st.analog), len(st.digital)
    af, df = afold_for(n_a), dfold_for(n_d)
    lead_fold = af if n_a else df
    # requested per-channel samples / rate
    if st.rate:
        samples = int(round(st.rate * st.duration))
    else:
        samples = int(st.samples)
    if samples > MAX_SAMPLES:
        warnings.append('samples capped at %d' % MAX_SAMPLES)
        samples = MAX_SAMPLES
    samples = max(samples, 1024)
    duration = st.duration
    # DSWave's auto rate is fold x the largest 1-2-5 table rate <= samples / fold / duration. When that is below
    # samples / duration, keep the requested window and record fewer samples (else the window grows up to 2.5x).
    # The tolerance keeps float results like 99,999,999.99 Hz from dropping a whole step.
    want = (st.rate if st.rate else samples / duration) / lead_fold
    rate = lead_fold * mso.snap_table_rate(want * (1 + 1e-9))
    if rate:
        if st.rate and rate * duration < 1024:      # explicit rate: keep it, the window grows to 1024 samples
            duration = 1024.0 / rate
            warnings.append('window lengthened to %.3g s (1024 samples minimum at %.3g Sa/s)' % (duration, rate))
        samples = max(min(samples, int(round(rate * duration))), 1024)
    storage = int(4 * math.ceil(samples / lead_fold / 64) * 64)
    time_ps = int(round(duration * 1e12))
    # DSWave truncates (1e12 / time) * (storage / 4) before snapping (mso._snap_rate): add a block when float
    # rounding would still land just below the intended rate
    for _ in range(4):
        if not rate or mso.snap_table_rate(1e12 / float(time_ps) * float(storage // 4)) * lead_fold >= rate:
            break
        storage += 4 * 64
    trig = {'enable': int(bool(st.trig_source) and st.trig_mode != 'force'),
            'mode': 1, 'ch_type': 0, 'ch_index': 0, 'type': EDGES.get(st.trig_edge, 0),
            'pulse_ps': int(round(st.trig_pulse * 1e12)), 'margin': int(min(max(st.sensitivity, 0), 15)),
            'hold_ps': int(round(st.holdoff * 1e12)), 'pos_pct': float(min(max(st.trig_pos, 0.0), 100.0))}
    if st.trig_source:
        trig['ch_type'] = 1 if st.trig_source.startswith('A') else 0
        trig['ch_index'] = int(st.trig_source[1:])
    elif st.analog:            # untriggered: the trigger domain must still have channels (it sets the depth)
        trig['ch_type'], trig['ch_index'] = 1, int(st.analog[0][1:])
    else:
        trig['ch_type'], trig['ch_index'] = 0, int(st.digital[0][1:])
    hal = {'analog': ana, 'digital': [int('D%d' % i in st.digital) for i in range(8)],
           'threshold_uv': int(round(min(max(st.threshold, 0.0), 5.0) * 1e6)),
           'sample_time_ps': time_ps, 'storage_depth': storage, 'auto_rate': 1,
           'trigger': trig, 'repeat': 0, 'roll': 0}
    return hal, scale, warnings, hints


# ------------------------------------------------------------------------------------------------ Scope
class Scope:
    def __init__(self, log=None):
        self.log = log
        self.dev = None
        self.engine = None
        self.lock = None
        self._last_cross = None      # CROSS tables written earlier in this session
        self._overdriven = False     # last capture saturated an analog input: let the front end recover first
        self._analog_awake = False   # the previous run of this session had analog channels
        import threading
        self._close_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def open(self, reload_fpga=False):
        """reload_fpga: reset and reconfigure the FPGA first (`openm454 recover`, see Device.open)."""
        from .lock import DeviceLock
        from .device import Device
        from .acquire import Engine
        self.lock = DeviceLock().acquire()
        dev = Device(log=self.log)
        try:
            dev.open(reload_fpga=reload_fpga)
        except BaseException:
            dev.close()
            self.lock.release()
            self.lock = None
            raise
        self.dev = dev
        self.engine = Engine(self.dev)
        return self

    def close(self):
        """Idempotent and safe to call from two threads (main thread and watchdog): only one closes."""
        with self._close_lock:
            dev, self.dev = self.dev, None
            lock, self.lock = self.lock, None
        if dev:
            dev.close()
        if lock:
            lock.release()

    def emergency_stop(self, wait=5.0):
        """Called from the watchdog thread. The engine is shut down for good (no further run can start) and a
        running capture stops through its own loop (normal stop handshake); registers are never written from here.
        If the capture does not stop in time nothing is sent and the process exit releases the handle."""
        eng = self.engine
        if eng is not None:
            eng.shutdown()
            if not eng.wait_idle(wait):
                return
        dev = self.dev
        if dev is not None and dev.t.running:
            return
        try:
            self.close()
        except Exception:
            pass

    def _check_link(self):
        sp = (getattr(self.dev, 'info', None) or {}).get('usb_speed', 'super')
        if sp not in ('super', 'super+'):
            raise DscopeError('UNSUPPORTED', 'the scope is connected at USB %s speed; only the USB 3 (SuperSpeed) '
                              'transfer profile is verified' % sp, 'plug the scope into a USB 3 port with a USB 3 cable')

    def _compile_ok(self, hal, warnings):
        """Compile; if the window needs the small-packet roll mode (never recorded from DSWave, not supported),
        raise the sample count until it does not."""
        zero = self.dev.zero_info if self.dev is not None else _cached_zero()
        prog, geom = _compile(hal, zero)
        n = 0
        limit = max(MAX_SAMPLES, geom['analog_depth'], geom['digital_depth'])
        while geom['small_pkt'] and n < 12 and 2 * max(geom['analog_depth'], geom['digital_depth']) <= limit:
            hal['storage_depth'] *= 2
            n += 1
            prog, geom = _compile(hal, zero)
        if geom['small_pkt']:
            raise DscopeError('INVALID_ARG', 'this window needs the small-packet stream mode, which is not supported',
                              'use more --samples or a higher --rate, a shorter --duration, or more channels')
        if n:
            warnings.append('sample count raised %dx: slower rates would need the unsupported small-packet stream '
                            'mode' % (2 ** n))
        return prog, geom

    @staticmethod
    def _check_complete(raw, geom):
        if raw.head is None or geom.get('repeat'):
            return True
        return not raw.timed_out and not raw.aborted and len(raw.data) >= geom['need_bytes']

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ info / awg
    def info(self):
        from . import awg
        i = dict(self.dev.info)
        i.pop('probe_ratio_raw', None)
        return {'device': dict(model='DSCope M454', **i),
                'limits': {'analog_channels': 4, 'digital_channels': 8, 'vdiv_v': [v / 1e6 for v in VDIVS_UV],
                           'max_rate_hz': {'analog': {'1ch': 400e6, '2ch': 200e6, '3-4ch': 100e6},
                                           'digital': {'1-2ch': 400e6, '3-4ch': 200e6, '5-8ch': 100e6}},
                           'max_samples_per_capture': MAX_SAMPLES, 'adc_bits': 8, 'vertical_divisions': 8,
                           'awg': {'v_min': 0.0, 'v_max': 3.0, 'f_max_hz': awg.F_MAX}},
                'awg_last': self.awg_status()}

    def awg_set(self, wave, freq, high, low, duty=50.0, phase=0.0):
        from . import awg
        awg.apply(self.dev.t, awg.program(wave, freq, high, low, duty, phase))
        awg.apply(self.dev.t, awg.START)
        st = dict(awg.describe(wave, freq, high, low, duty, phase), enabled=True)
        self._save_awg(st)
        return st

    def awg_off(self):
        from . import awg
        awg.apply(self.dev.t, awg.STOP + awg.PARK)
        st = dict(self.awg_status() or {}, enabled=False, output_v=0.0)
        self._save_awg(st)
        return st

    def awg_dc(self, level):
        """Steady DC level on the AWG output (0..3 V, steps of 1/156 V)."""
        from . import awg
        awg.apply(self.dev.t, awg.dc(level))
        st = {'wave': 'dc', 'level_v': round(int(round(level * 156)) / 156, 4), 'enabled': True}
        self._save_awg(st)
        return st

    def awg_sweep(self, start, end, time, sweep_type='linear', wave='sine', high=3.0, low=0.0, duty=50.0):
        """Frequency sweep from `start` to `end` Hz in `time` s (repeats); register program from DSWave's HAL."""
        from . import awg, awg_modes
        direction = 'up' if end > start else 'down'
        awg.apply(self.dev.t, awg_modes.program_sweep(start, end, time, sweep_type, direction, wave=wave, high=high,
                                                      low=low, duty=duty))
        awg.apply(self.dev.t, awg.START)
        st = {'mode': 'sweep', 'wave': wave, 'start_hz': start, 'end_hz': end, 'time_s': time, 'sweep_type': sweep_type,
              'high_v': high, 'low_v': low, 'enabled': True}
        self._save_awg(st)
        return st

    def awg_burst(self, wave, freq, high, low, cycles, period, duty=50.0, phase=0.0):
        """`cycles` periods of the carrier every `period` s (internal trigger)."""
        from . import awg, awg_modes
        awg.apply(self.dev.t, awg_modes.program_burst(wave, freq, high, low, duty, phase, cycles=cycles, period=period))
        awg.apply(self.dev.t, awg.START)
        st = {'mode': 'burst', 'wave': wave, 'freq_hz': freq, 'cycles': cycles, 'period_s': period, 'high_v': high,
              'low_v': low, 'enabled': True}
        self._save_awg(st)
        return st

    def awg_modulate(self, kind, wave, freq, high, low, mod_wave='sine', mod_freq=100.0, depth=50.0, deviation=None,
                     hop=None, duty=50.0):
        """AM (depth %), FM (deviation Hz) or FSK (hop Hz) of a carrier, internal modulation source."""
        from . import awg, awg_modes
        awg.apply(self.dev.t, awg_modes.program_modulation(kind, wave, freq, high, low, duty, mod_wave=mod_wave,
                                                           mod_freq=mod_freq, depth=depth, deviation=deviation, hop=hop))
        awg.apply(self.dev.t, awg.START)
        st = {'mode': 'modulation', 'kind': kind, 'wave': wave, 'freq_hz': freq, 'mod_wave': mod_wave,
              'mod_freq_hz': mod_freq, 'high_v': high, 'low_v': low, 'enabled': True}
        st.update({'am': {'depth_pct': depth}, 'fm': {'deviation_hz': deviation}, 'fsk': {'hop_hz': hop}}.get(kind, {}))
        self._save_awg(st)
        return st

    def awg_from_spec(self, spec):
        from .units import si
        p = spec.split(',')
        wave = p[0]; freq = si(p[1], 'Hz', 'awg freq') if len(p) > 1 else 1000
        low = si(p[2], 'V', 'awg low') if len(p) > 2 else 0.0
        high = si(p[3], 'V', 'awg high') if len(p) > 3 else 3.0
        duty = si(p[4], None, 'awg duty') if len(p) > 4 else 50.0
        return self.awg_set(wave, freq, high, low, duty)

    def awg_status(self):
        try:
            return json.load(open(os.path.join(STATE, 'awg.json')))
        except (OSError, ValueError):
            return None

    def _save_awg(self, st):
        os.makedirs(STATE, exist_ok=True)
        json.dump(st, open(os.path.join(STATE, 'awg.json'), 'w'))

    # ------------------------------------------------------------ capture
    def plan(self, st):
        hal, scale, warnings, hints = hal_config(st)
        hal['trigger']['enable'] = int(bool(st.trig_source) and st.trig_mode != 'force')
        prog, geom = self._compile_ok(hal, warnings)
        return {'hal': hal, 'geometry': {k: v for k, v in geom.items() if k != '_regs'}, 'writes': len(prog),
                'program': ['%02X=%04X' % w for w in prog[:400]], 'warnings': warnings}

    @staticmethod
    def _head_wait(geom, wait):
        """Time from the start writes until the head can arrive: outside roll mode the head comes only after the
        whole record has been acquired (pre-trigger fill + trigger + post-trigger), plus transfer latency."""
        return wait + (0.0 if geom.get('roll') else geom.get('record_s', 0.0)) + 0.5

    def capture(self, st):
        self._check_link()
        hal, scale, warnings, hints = hal_config(st)
        t_begin = time.time()
        attempts = []
        mode = st.trig_mode
        triggered_wanted = bool(st.trig_source) and mode != 'force'
        wait = (st.auto_after if mode == 'auto' else st.timeout) + max(st.holdoff, 0.0)   # holdoff delays the first trigger
        hal['trigger']['enable'] = int(triggered_wanted)
        prog, geom = self._compile_ok(hal, warnings)
        collect = max(10.0, 1.5 * geom['record_s'] + 2.0)
        raw = self._run(prog, geom, self._head_wait(geom, wait if triggered_wanted else 2.0), collect)
        attempts.append(('triggered' if triggered_wanted else 'forced') if raw.head else 'no-trigger')
        if raw.head is None:
            self._overdriven = True             # no data to check: an input may have been saturated while waiting
            if raw.aborted:
                raise DscopeError('TIMEOUT_WATCHDOG', 'capture aborted before the trigger')
            if mode == 'normal':
                raise DscopeError('TIMEOUT_NO_TRIGGER', 'no trigger within %.3g s on %s (%.3g s wait + %.3g s record)'
                                  % (self._head_wait(geom, wait), st.trig_source, wait, geom['record_s']),
                                  'check the trigger level/edge or use --trig-mode auto')
            hal['trigger']['enable'] = 0
            prog, geom = self._compile_ok(hal, warnings)
            collect = max(10.0, 1.5 * geom['record_s'] + 2.0)
            raw = self._run(prog, geom, self._head_wait(geom, 2.0), collect)
            attempts.append('forced')
            if raw.head is None:
                raise DscopeError('PROTOCOL_ERROR', 'device returned no data even for a forced capture',
                                  'run "openm454 release"; if it persists replug the scope')
        if not self._check_complete(raw, geom):
            raise DscopeError('TIMEOUT_DATA', 'incomplete record: %d of %d data bytes (%s)' % (
                len(raw.data), geom['need_bytes'], 'aborted' if raw.aborted else 'data stopped arriving'),
                'retry once; if it repeats, check the USB 3 connection')
        frame = _decode(raw, geom, hal)
        self._overdriven = _saturated(frame)
        check, misfires = 'unchecked', 0
        if attempts[-1] == 'triggered' and trigcheck.checked_edge(st.trig_edge) and st.trig_source.startswith('D'):
            raw, frame, check, misfires = self._edge_retries(st, hal, prog, geom, collect, wait, raw, frame,
                                                             attempts, warnings, hints)
        meta = self._meta(st, hal, scale, geom, frame, raw, warnings, hints, attempts, t_begin)
        meta['acq']['trigger_check'] = check
        meta['acq']['trigger_misfires'] = misfires
        cap = Capture(meta, frame['analog'], frame['digital'])
        cap.meta['hints'] = _hints(cap) + hints
        return cap

    def _edge_retries(self, st, hal, prog, geom, collect, wait, raw, frame, attempts, warnings, hints):
        """Digital rise/fall trigger (see openm454.trigcheck): a capture whose settled levels around t = 0 show the
        opposite edge is a misfire on comparator chatter; it is discarded and the trigger re-armed, at most
        st.trig_retries times and only while the trigger wait of the request (`wait`, counted over all runs) lasts
        (a re-armed run waits for the holdoff again).
        -> (raw, frame, check, misfires discarded); check 'ok' | 'misfire_retried' | 'mismatch' | 'inconclusive'
        ('unchecked' when the head reports no trigger)."""
        def verdict(raw, frame):
            rate = geom.get('digital_rate')
            bits = frame['digital'].get(st.trig_source)
            if not raw.triggered:
                return 'unchecked'
            if bits is None or not rate:
                return 'inconclusive'
            return trigcheck.edge_check(bits, int(round(-frame['t0_digital'] * rate)), rate, st.trig_edge)
        check = verdict(raw, frame)
        budget = wait - _trigger_wait(raw, geom)
        retries, tries, misfires, why = max(int(st.trig_retries or 0), 0), 0, 0, None
        while check == 'mismatch':
            attempts[-1] = 'misfire'
            why = ('not re-armed: trig_retries=0' if not retries else
                   'all %d re-arms (trig_retries) used' % retries if tries >= retries else
                   'the %.3g s trigger wait is used up' % wait if budget <= max(st.holdoff, 0.0) else None)
            if why:
                break
            tries += 1
            nxt = self._run(prog, geom, self._head_wait(geom, budget), collect)
            if nxt.head is None:
                self._overdriven = True
                if nxt.aborted:
                    raise DscopeError('TIMEOUT_WATCHDOG', 'capture aborted while re-arming after a trigger misfire')
                attempts.append('no-trigger')
                why = 'the re-armed trigger did not fire within the remaining %.3g s' % budget
                break
            attempts.append('triggered')
            if not self._check_complete(nxt, geom):
                raise DscopeError('TIMEOUT_DATA', 'incomplete record: %d of %d data bytes (%s)' % (
                    len(nxt.data), geom['need_bytes'], 'aborted' if nxt.aborted else 'data stopped arriving'),
                    'retry once; if it repeats, check the USB 3 connection')
            budget -= _trigger_wait(nxt, geom)
            raw, frame, misfires = nxt, _decode(nxt, geom, hal), misfires + 1
            self._overdriven = _saturated(frame)
            check = verdict(raw, frame)
        edge, other = ('falling', 'rising') if trigcheck.checked_edge(st.trig_edge) == 'fall' else ('rising', 'falling')
        name = '%s %s-edge trigger' % (st.trig_source, edge)
        if check == 'mismatch':
            warnings.append('%s fired inside a %s edge (comparator chatter), so t = 0 is not a %s edge; %s (%s)'
                            % (name, other, edge, 'all %d captures misfired, the last is returned' % (misfires + 1)
                               if misfires else 'the capture is returned anyway', why))
            hints.append('%s misfired: %s' % (name, trigcheck.HINT))
        elif misfires:
            hints.append('%s: %d misfire(s) inside a %s edge were discarded and re-armed (comparator chatter, no '
                         'hysteresis); the other edge or an analog trigger avoids them' % (name, misfires, other))
            if check == 'ok':
                check = 'misfire_retried'
        return raw, frame, check, misfires

    def _run(self, prog, geom, trigger_timeout, collect_timeout, **kw):
        """Engine run with CROSS-table caching: the 4 x 257 CROSS writes are skipped when identical to the
        ones written earlier in this session (the first capture of every session always writes them)."""
        cross = [w for w in prog if w[0] in CROSS_REGS]
        send = prog
        if cross and cross == self._last_cross:
            send = [w for w in prog if w[0] not in CROSS_REGS]
        settle = OVERDRIVE_SETTLE if self._overdriven else 0.0
        analog_on = bool(geom.get('analog_rate'))
        if analog_on and not self._analog_awake:     # analog side idle before (or unknown): front end recovers
            settle = max(settle, ANALOG_WAKE_SETTLE)
        pkt = geom['pkt_bytes']
        if not geom.get('repeat'):        # repeat: one frame per transfer, a larger read would merge two frames
            # Linux/xHCI: an EP 0x86 read that is not a multiple of the max packet ends in -EOVERFLOW as soon as a
            # 1024-byte packet no longer fits the remaining buffer (the stream is byte-continuous, so a larger read
            # is harmless)
            pkt = (pkt + EP_MAX_PACKET - 1) // EP_MAX_PACKET * EP_MAX_PACKET
        # The three caches are committed only after the engine ran: a run that fails must not make the next run
        # skip the front-end waits or forget an overdriven input. Only the usbfs precheck (USB_NO_MEM when not even
        # the minimum queue fits) refuses before the setup program; a failed write, a USB_ERROR or USB_NO_MEM at
        # submit, a Ctrl+C or an on_chunk/stop_flag exception all come after it.
        try:
            raw = self.engine.run(send, pkt_bytes=pkt, need_bytes=geom['need_bytes'],
                                  n_transfers=geom.get('transfer_count', 16), trigger_timeout=trigger_timeout,
                                  collect_timeout=collect_timeout, settle=settle, **kw)
        except BaseException:
            self._last_cross = None       # unknown how far the writes got: rewrite the CROSS tables next time
            if not analog_on:             # a digital-only setup program may already have idled the analog front end
                self._analog_awake = False
            raise
        self._last_cross = cross or None
        self._overdriven = False
        self._analog_awake = analog_on
        return raw

    def stream(self, st, every=1.0, emit=None, stop_flag=None, build=True, live=True):
        """Long untriggered recording with live statistics: the device streams the record while it is
        acquired (roll mode; live=False: only below ~100 MB/s). emit(dict) receives one line per `every` seconds
        of signal time. stop_flag(): optional callable that ends the recording early (normal stop).
        Returns the full Capture at the end. build=False (a stream refreshing record after record): a complete
        record is not decoded into a Capture; the returned one only carries meta['acq'] (samples, duration,
        start time), so the next record can start at once; build_capture() decodes it later if it is needed after
        all. build may be a callable, evaluated when the record is complete (a stop request may arrive meanwhile).
        live=True: the record is sent while it is acquired at any data rate (MODE 2; DSWave does that only below
        102.4 MB/s and records first above it, which live=False keeps); the scope's memory buffers what the USB link
        cannot take, so the next record starts after a short drain instead of a full read-out."""
        from . import packet
        self._check_link()
        st.trig_source, st.trig_mode = None, 'force'
        hal, scale, warnings, hints = hal_config(st)
        hal['trigger']['enable'] = 0
        hal['stream_always'] = int(bool(live))
        prog, geom = self._compile_ok(hal, warnings)
        layout = packet.layout_from_regs(geom['_regs'], rates={'analog': geom['analog_rate'], 'digital': geom['digital_rate']},
                                         trig_domain=geom['trig_domain'])
        asm = packet.StreamAssembler(layout, packed=True)      # digital stays packed: statistics on the bits
        an, dg = list(st.analog), list(st.digital)
        acc = {}                                                 # per line: running min/max/sum, ones/edges
        last_bit = {c: None for c in dg}
        state = {'t_sig': 0.0, 'next': every, 'i': 0}
        prate = geom['analog_rate'] if an else geom['digital_rate']
        t0 = time.time()

        def pick(d, c):
            if c in d:
                return d[c]
            return d.get(int(c[1:]))

        def report(final=False):
            line = {'i': state['i'], 't_signal_s': round(state['t_sig'], 4), 't_wall_s': round(time.time() - t0, 3)}
            for c in an:
                a = acc.get(c)
                if a:
                    s_ = scale[c]
                    v = lambda code: round(float((code - s_['zero_code']) * s_['volts_per_code'] * s_['probe']), 4)
                    line[c] = {'vmin_v': v(a[0]), 'vmax_v': v(a[1]), 'vmean_v': v(a[2] / a[3]), 'n': a[3]}
            for c in dg:
                a = acc.get(c)
                if a:
                    line[c] = {'high_frac': round(a[0] / a[2], 4), 'edges': a[1]}
            acc.clear()
            state['i'] += 1
            if final:
                line['final'] = True
            if emit:
                emit(line)

        def on_chunk(buf):
            handle(asm.feed(buf))

        def handle(events):
            for ev in events:
                if ev['kind'] != 'samples':
                    continue
                for c in an:
                    x = pick(ev['analog'], c)
                    if x is not None and x.size:
                        a = acc.setdefault(c, [255, 0, 0, 0])
                        a[0], a[1] = min(a[0], int(x.min())), max(a[1], int(x.max()))
                        a[2] += int(x.sum(dtype=np.uint64))
                        a[3] += int(x.size)
                for c in dg:
                    x = pick(ev['digital'], c)
                    if x is not None and x.size:
                        ones, edges, last_bit[c] = packet.bit_stats(x, last_bit[c])
                        a = acc.setdefault(c, [0, 0, 0])
                        a[0] += ones
                        a[1] += edges
                        a[2] += ev['nblocks'] * layout.digital_block_samples
                n = ev['nblocks'] * (layout.analog_block_samples if an else layout.digital_block_samples)
                state['t_sig'] += n / prate
                if state['t_sig'] >= state['next']:
                    report()
                    state['next'] += every

        rec = geom['record_s']
        raw = self._run(prog, geom, self._head_wait(geom, 5.0), 1.5 * rec + 5.0, on_chunk=on_chunk, stop_flag=stop_flag)
        handle(asm.flush())                 # blocks of a record that ended early (stop request, timeout)
        report(final=True)
        if raw.head is None:
            if raw.stopped:
                raise DscopeError('STOPPED', 'stream stopped on request before any data arrived')
            if raw.aborted:
                raise DscopeError('TIMEOUT_WATCHDOG', 'stream stopped before any data arrived')
            raise DscopeError('PROTOCOL_ERROR', 'no data from the device')
        complete = self._check_complete(raw, geom)
        if not complete:
            warnings.append('record incomplete: %d of %d data bytes (%s)' % (
                len(raw.data), geom['need_bytes'], 'stopped on request' if raw.stopped else
                'aborted' if raw.aborted else 'data stopped arriving'))
        def full():
            frame = _decode(raw, geom, hal)
            self._overdriven = _saturated(frame)
            meta = self._meta(st, hal, scale, geom, frame, raw, warnings, hints, ['stream'], t0)
            meta['acq']['mode'] = 'roll' if geom.get('roll') else 'single'
            meta['acq']['t_start_wall'] = raw.t_start  # host clock at the start write (stream refresh: seam gaps)
            cap = Capture(meta, frame['analog'], frame['digital'])
            cap.meta['hints'] = _hints(cap) + hints
            return cap
        if complete and not (build() if callable(build) else build):
            dom = 'analog' if st.analog else 'digital'
            n = geom[dom + '_depth']
            light = Capture({'kept': False, 'warnings': warnings, 'acq': {
                'mode': 'roll' if geom.get('roll') else 'single', 'samples': n, 'duration_s': n / geom[dom + '_rate'],
                't_start_wall': raw.t_start, 'complete': True, 'stop': raw.stop, **queue_depth(raw)}})
            light.build = full
            return light
        return full()

    @staticmethod
    def build_capture(cap):
        """The full Capture of a record that stream(build=False) did not decode (the raw data was kept)."""
        return cap.build() if not cap.meta.get('kept', True) else cap

    def record(self, st, rate, duration=None, every=1.0, out_stem=None, storage='ram', ram_budget=None, chunk_s=None,
            emit=None, stop_flag=None, heartbeat=None, keep_last=False, force=False, probe=None):
        """Gap-free recording over loop mode at one `rate` for every channel (<= 100 MSa/s; 200/400 MSa/s
        interleaved on 1-2 channels, see logger.max_rate), `duration` s or until
        stop_flag() is true (duration None). storage 'ram' (default: kept in RAM, written once to <out_stem>.npz),
        'disk' (chunks dumped while running), 'none'. Returns the result document (see openm454.logger).
        probe: None measures the readout of this channel set first when it is unknown and the rate is above the
        sustained one; True always; False never (plans with the cached readout or the 190 MB/s ceiling).
        Raises STREAM_BETTER / NEEDS_FORCE (with .plan) for a log longer than its gap-free limit."""
        from . import logger
        self._check_link()
        out_dir = os.path.dirname(out_stem) if out_stem else None
        kw = dict(zero_info=self.dev.zero_info, force=force, keep_last=keep_last)
        pl = logger.plan(st, rate, duration, every, out_dir, storage, ram_budget, chunk_s, defer=probe is not False,
                         remeasure=probe is True, **kw)
        if pl['_needs_probe']:
            logger.measure_readout(self, pl)
            pl = logger.plan(st, rate, duration, every, out_dir, storage, ram_budget, chunk_s, **kw)
        return logger.run(self, st, pl, out_stem, emit, stop_flag, heartbeat)

    def _meta(self, st, hal, scale, geom, frame, raw, warnings, hints, attempts, t_begin):
        trig = {'source': st.trig_source, 'edge': st.trig_edge if st.trig_source else None,
                'mode': st.trig_mode, 'pos_pct': hal['trigger']['pos_pct']}
        if st.trig_source and st.trig_source.startswith('A'):
            s = scale[st.trig_source]
            code = hal['analog'][int(st.trig_source[1])]['trig_level_code']
            trig['level_v'] = round((code - s['zero_code']) * s['volts_per_code'] * s['probe'], 5)
        ch_scale = {c: scale[c] for c in st.analog}
        applied = {'channels': st.analog + st.digital,
                   'vdiv_v': {c: round(scale[c]['vdiv_v'] * scale[c]['probe'], 6) for c in st.analog},
                   'offset_v': {c: round((128 - scale[c]['zero_code']) * scale[c]['volts_per_code'] * scale[c]['probe'], 5) for c in st.analog},
                   'probe': {c: scale[c]['probe'] for c in st.analog},
                   'coupling': {c: st.coupling.get(c, 'dc') for c in st.analog},
                   'bw20': st.bw20, 'invert': st.invert, 'threshold_v': hal['threshold_uv'] / 1e6,
                   'trigger': trig, 'duration_req_s': st.duration}
        n_a = max([len(v) for v in frame['analog'].values()] + [0])
        n_d = max([len(v) for v in frame['digital'].values()] + [0])
        # samples / duration_s describe the analog channels when there are any (their rate can differ from the
        # digital one in mixed captures); digital_samples is the per-channel digital count
        n, rate = (n_a, geom.get('analog_rate')) if frame['analog'] else (n_d, geom.get('digital_rate'))
        acq = {'analog_rate_hz': geom.get('analog_rate'), 'digital_rate_hz': geom.get('digital_rate'),
               'samples': n, 'duration_s': n / rate if rate else 0.0,
               'triggered': bool(raw.triggered), 'attempts': attempts,
               'stop': getattr(raw, 'stop', None), **queue_depth(raw),
               'handshakes': len(getattr(raw, 'handshakes', [])),
               'complete': self._check_complete(raw, geom),
               'digital_samples': n_d if frame['digital'] else None,
               'elapsed_ms': round((time.time() - t_begin) * 1000)}
        return {'version': 1, 'device': 'DSCope M454', 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'applied': applied, 'acq': acq, 'channels': ch_scale,
                'analog_rate_hz': geom.get('analog_rate'), 'digital_rate_hz': geom.get('digital_rate'),
                't0_analog_s': frame['t0_analog'], 't0_digital_s': frame['t0_digital'],
                'trigger': trig, 'warnings': warnings, 'geometry': {k: v for k, v in geom.items() if isinstance(v, (int, float, str))}}

    def debug(self, args):
        from .units import si
        t = self.dev.t
        if args.debug_cmd == 'regs-read':
            d = t.read(int(args.addr, 0), int(args.len, 0), int(args.ext, 0))
            return {'addr': args.addr, 'hex': d.hex()}
        if args.debug_cmd == 'regs-write':
            addr = int(args.addr, 0)
            if addr in (0x00, 0x01, 0x02, 0x03):
                raise DscopeError('UNSAFE_OPERATION', 'MODE/CTRL/ACTION/SYNC are only written by the acquisition '
                                  'engine (a run must be started with queued reads and stopped with a handshake)')
            t.write(addr, int(args.value, 0))
            return {'written': [args.addr, args.value]}
        return {}


CROSS_REGS = (0x44, 0x54, 0x64, 0x74)


def _trigger_wait(raw, geom):
    """Seconds a run waited for its trigger: head arrival - start - record time (outside roll mode the head comes
    after the whole record); 0 when the times are unknown."""
    t0, th = getattr(raw, 't_start', None), getattr(raw, 't_head', None)
    if t0 is None or th is None:
        return 0.0
    return max(0.0, th - t0 - (0.0 if geom.get('roll') else geom.get('record_s', 0.0)))


def queue_depth(raw):
    """acq fields for the EP 0x86 queue: transfers = the reads queued at once; transfers_wanted (the geometry's
    depth) only when the engine queued fewer, i.e. a Linux usbfs budget that could not hold them all."""
    n, want = getattr(raw, 'transfers', None), getattr(raw, 'transfers_wanted', None)
    lowered = n is not None and want is not None and n < want
    return {'transfers': n, 'transfers_wanted': want} if lowered else {'transfers': n}


def _saturated(frame):
    """True when an analog input sat at the ADC limits for > 1 % of the record (heavy overdrive). The front end
    then needs up to ~0.2 s to recover (measured 2026-10-31: 2.8 V into 100 mV/div, then 1 V/div read +0.24 V for the
    first 20 ms), so the next run waits OVERDRIVE_SETTLE after its setup writes."""
    for codes in frame['analog'].values():
        n = codes.size
        if n and (np.count_nonzero(codes >= 252) + np.count_nonzero(codes <= 3)) > 0.01 * n:
            return True
    return False


def _hints(cap):
    out = []
    for ch in cap.analog:
        codes = cap.analog[ch]
        if codes.size == 0:
            continue
        hi, lo = np.mean(codes >= CODE_MAX - 1), np.mean(codes <= CODE_MIN + 1)
        span = int(codes.max()) - int(codes.min())
        if hi > 0.001:
            out.append('%s clips at the top: increase --vdiv or lower --offset' % ch)
        if lo > 0.001:
            out.append('%s clips at the bottom: increase --vdiv or raise --offset' % ch)
        m = cap.measure(ch) if codes.size <= 20_000_000 else {}      # measured (and cached) once for the summary
        if m.get('freq_hz') is None and (m.get('edges_rise', 0) + m.get('edges_fall', 0)) >= 1:
            out.append('%s: fewer than 2 periods in the window; use a longer --duration/--timebase to measure frequency' % ch)
        if span < 2 * CODES_PER_DIV and not hi and not lo and span > 3:
            out.append('%s uses only %.1f of 8 divisions: a smaller --vdiv gives more resolution' % (ch, span / CODES_PER_DIV))
    return out


def _cached_zero():
    """Calibration for offline compiles (validation, budgets, `debug plan`): the cached zero info if it parses,
    else a neutral default (VGA table from the profile). Captures always use the device's own calibration."""
    from . import mso
    p = os.path.join(STATE, 'cache', 'zero_info_from_trace.bin')
    try:
        blob = open(p, 'rb').read()
        mso.parse_zero_info(blob)
        return blob
    except (OSError, ValueError):
        return [mso.ProbeCal(pwm_scale=485, adc_trim=0, extra=0, vga=[]) for _ in range(4)]


# ------------------------------------------------------------------------------------------------ adapters
def _compile(hal, zero_info):
    """-> (program [(addr, val)], geometry dict with pkt_bytes, need_bytes, transfer_count, analog_rate,
    digital_rate, ...). Bound to openm454.mso."""
    from . import mso_adapter
    return mso_adapter.compile(hal, zero_info)


def _decode(raw, geom, hal):
    """-> {'analog': {ch: uint8}, 'digital': {ch: uint8}, 't0_analog': s, 't0_digital': s}. Bound to openm454.packet."""
    from . import mso_adapter
    return mso_adapter.decode(raw, geom, hal)
