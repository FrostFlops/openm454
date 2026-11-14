"""Capture container: raw codes + scaling + timing, measurements, npz save/load.

Time base: t = 0 at the trigger point (or at the window centre for untriggered captures);
sample k of a channel sampled at rate r is at t = t0 + k / r.
Analog volts = (code - zero_code) * volts_per_code * probe, volts_per_code = vdiv * 8 / 235.
"""
import json
import os
import re
import time

import numpy as np

from . import measure

CODES_PER_DIV = 235.0 / 8.0          # 29.375
CODE_MIN, CODE_MAX = 10, 245          # usable ADC range (profile ref_min/ref_max)


class Capture:
    def __init__(self, meta, analog=None, digital=None):
        self.meta = meta                   # dict (JSON-serialisable)
        self.analog = analog or {}         # 'A1' -> uint8 codes
        self.digital = digital or {}       # 'D0' -> uint8 0/1
        self.frames = None                 # repeat mode: one decoded dict per frame, the first one also in .analog
        self._measured = {}                # (ch, edges_n, window) -> result (the data never changes)

    # ---------------------------------------------------------------- scaling / time
    def scale(self, ch):
        return self.meta['channels'][ch]

    def volts(self, ch):
        s = self.scale(ch)
        return (self.analog[ch].astype(np.float32) - s['zero_code']) * s['volts_per_code'] * s.get('probe', 1.0)

    def rate(self, ch):
        return self.meta['analog_rate_hz'] if ch.startswith('A') else self.meta['digital_rate_hz']

    def t0(self, ch):
        return self.meta['t0_analog_s'] if ch.startswith('A') else self.meta['t0_digital_s']

    def time(self, ch):
        n = len(self.analog[ch]) if ch.startswith('A') else len(self.digital[ch])
        return self.t0(ch) + np.arange(n) / self.rate(ch)

    # ---------------------------------------------------------------- measurements
    def measure(self, ch, edges_n=0, window=None):
        key = (ch, edges_n, tuple(window) if window else None)
        if key not in self._measured:
            self._measured[key] = self._measure(ch, edges_n, window)
        return dict(self._measured[key])

    def _measure(self, ch, edges_n=0, window=None):
        """measure.analog/digital on the channel (or a time window of it); edges_rise_s / edges_fall_s are converted
        to the capture's time base (t = 0 at the trigger), like time() and dump()."""
        a = self._win_start(ch, window)
        if ch.startswith('A'):
            v = self.volts(ch)
            s = self.scale(ch)
            k = s.get('probe', 1.0) * s['volts_per_code']
            if window:
                v = self._win(ch, v, window)
            res = measure.analog(v, self.rate(ch), clip_lo=(CODE_MIN + 1 - s['zero_code']) * k,
                                 clip_hi=(CODE_MAX - 1 - s['zero_code']) * k, edges_n=edges_n, lsb=k)
        else:
            b = self.digital[ch]
            if window:
                b = self._win(ch, b, window)
            res = measure.digital(b, self.rate(ch), edges_n=edges_n)
        t_first = self.t0(ch) + a / self.rate(ch)
        for key in ('edges_rise_s', 'edges_fall_s'):
            if key in res:
                res[key] = [float('%.10g' % (t_first + x)) for x in res[key]]
        return res

    def _win_start(self, ch, window):
        if not window or window[0] is None:
            return 0
        return max(0, int((window[0] - self.t0(ch)) * self.rate(ch)))

    def _win(self, ch, arr, window):
        t0, t1 = window
        r, s0 = self.rate(ch), self.t0(ch)
        a = max(0, int((t0 - s0) * r)) if t0 is not None else 0
        b = min(len(arr), int((t1 - s0) * r)) if t1 is not None else len(arr)
        return arr[a:b]

    def channels(self):
        return sorted(self.analog) + sorted(self.digital, key=lambda c: int(c[1:]))

    def summary(self, edges_n=0, window=None, channels=None):
        """channels: optional collection of channel names; the others are not measured (deep records measure
        slowly, ~4 s per 100M-sample channel)."""
        out = {'analog': {}, 'digital': {}}
        for ch in sorted(self.analog):
            if channels is None or ch in channels:
                out['analog'][ch] = self.measure(ch, edges_n, window)
        for ch in sorted(self.digital, key=lambda c: int(c[1:])):
            if channels is None or ch in channels:
                out['digital'][ch] = self.measure(ch, edges_n, window)
        return out

    def decode(self, spec, window=None, limit=None):
        """Protocol decoder on captured lines, e.g. 'uart:rx=D4,baud=115200' (see openm454.decode)."""
        from .decode import decode
        return decode(self, spec, window, limit)

    def dump(self, ch, points=200, window=None):
        """Decimated min/max/mean series for numeric inspection without a picture."""
        arr = self.volts(ch) if ch.startswith('A') else self.digital[ch].astype(np.float32)
        t = self.time(ch)
        if window:
            arr = self._win(ch, arr, window); t = self._win(ch, t, window)
        n = len(arr)
        if n == 0:
            return {'t_s': [], 'min': [], 'max': [], 'mean': []}
        k = max(1, int(np.ceil(n / points)))
        m = (n // k) * k
        a = arr[:m].reshape(-1, k)
        tt = t[:m].reshape(-1, k)[:, 0]
        r = lambda x: [float('%.5g' % y) for y in x]
        return {'t_s': r(tt), 'min': r(a.min(1)), 'max': r(a.max(1)), 'mean': r(a.mean(1)), 'samples_per_point': k}

    # ---------------------------------------------------------------- persistence
    def save(self, path):
        arrays = {}
        for ch, a in self.analog.items():
            arrays['analog_' + ch] = a
        for ch, a in self.digital.items():
            arrays['digital_' + ch] = np.packbits(a, bitorder='little')
            self.meta.setdefault('digital_lengths', {})[ch] = int(len(a))
        meta = json.dumps(self.meta)
        n = sum(len(a) for a in self.analog.values())
        (np.savez_compressed if n < 20_000_000 else np.savez)(path, meta=np.frombuffer(meta.encode(), dtype=np.uint8), **arrays)
        return path

    def to_volts_dict(self):
        return {ch: self.volts(ch) for ch in self.analog}


def load_capture(path):
    z = np.load(path)
    meta = json.loads(bytes(z['meta']).decode())
    analog, digital = {}, {}
    for k in z.files:
        if k.startswith('analog_'):
            analog[k[7:]] = z[k]
        elif k.startswith('digital_'):
            ch = k[8:]
            digital[ch] = np.unpackbits(z[k], bitorder='little')[:meta['digital_lengths'][ch]]
    return Capture(meta, analog, digital)


def new_path(out_dir, tag='cap'):
    os.makedirs(out_dir, exist_ok=True)
    base = time.strftime('%Y%m%d-%H%M%S') + ('-%03d' % (int(time.time() * 1000) % 1000))
    return os.path.join(out_dir, '%s_%s' % (base, tag))


CAPTURE_NAME = re.compile(r'^(\d{8}-\d{6}-\d{3}_[^\\/]+?)(_decode\.json|\.npz|\.png)$')   # new_path() + extension


def prune(out_dir, keep=200, tag=None):
    """Keep the newest `keep` captures in out_dir. Only files named the way new_path() names them (with .npz, .png or
    _decode.json), and with tag only that tag's, are ever removed: other files in a user's --out folder, and log or
    stream recordings saved there under their own tags, are left alone."""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    groups = {}
    for n in names:
        m = CAPTURE_NAME.match(n)
        if m and (tag is None or m.group(1).split('_', 1)[1] == tag):
            p = os.path.join(out_dir, n)
            try:
                groups.setdefault(m.group(1), []).append((os.path.getmtime(p), p))
            except OSError:
                continue
    stems = sorted(groups, key=lambda s: max(t for t, _ in groups[s]))
    for st in stems[:-keep] if keep > 0 else stems:
        for _, p in groups[st]:
            try:
                os.remove(p)
            except OSError:
                pass
