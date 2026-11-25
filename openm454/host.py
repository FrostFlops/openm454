"""This PC's limits for recording: what the owner allows (RAM, disk, data folder) and what was measured.

Stored in state/host.json (git-ignored). `python -m openm454 bench` measures and records it; `log` takes its RAM budget,
disk allowance and default output folder from here.

  allow: {'ram_bytes', 'disk_bytes', 'data_dir'}          set by the owner (bench --ram / --disk / --dir)
  bench: {'time', 'ram_free_bytes', 'decode_mb_s', 'disk_write_mb_s', 'disk_dir', 'usb_readout_mb_s', ...}
"""
import json
import os
import shutil
import tempfile
import time

import numpy as np

from .paths import ROOT
HOST_FILE = os.path.join(ROOT, 'state', 'host.json')


def load():
    try:
        with open(HOST_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d):
    os.makedirs(os.path.dirname(HOST_FILE), exist_ok=True)
    tmp = HOST_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, HOST_FILE)


def allow():
    return load().get('allow', {})


def data_dir(default):
    return allow().get('data_dir') or default


def bench_decode(seconds=1.0):
    """Throughput of the log writer's per-block work (decode + statistics) on 4 analog + 8 digital blocks, MB/s."""
    from . import packet
    lay = packet.make_layout(analog=(0, 1, 2, 3), digital=tuple(range(8)), analog_rate=1e6, digital_rate=1e6,
                             afold=1, dfold=1, acq_words=None)
    nb = (64 << 20) // lay.block_bytes
    B = np.random.default_rng(0).integers(0, 256, (nb, lay.block_bytes), dtype=np.uint8)
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < seconds:
        an, dg = packet._decode_blocks(B, lay, packed=True)
        for x in an.values():
            int(x.min()), int(x.max()), int(x.sum(dtype=np.uint64))
        for x in dg.values():
            packet.bit_stats(x)
        n += B.nbytes
    return n / (time.perf_counter() - t0) / 1e6


def bench_disk(d, mb=256):
    """Sequential write speed into folder d (fsync'ed), MB/s; the test file is deleted. Writes `mb` MB once."""
    os.makedirs(d, exist_ok=True)
    block = np.random.default_rng(1).integers(0, 256, 8 << 20, dtype=np.uint8).tobytes()
    fd, path = tempfile.mkstemp(prefix='openm454_bench_', suffix='.bin', dir=d)
    try:
        t0 = time.perf_counter()
        with os.fdopen(fd, 'wb', buffering=0) as f:
            for _ in range(max(1, mb // 8)):
                f.write(block)
            os.fsync(f.fileno())
        dt = time.perf_counter() - t0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return (max(1, mb // 8) * len(block)) / dt / 1e6, shutil.disk_usage(d).free


def readout_key(n_analog, n_digital, rate):
    """The loop readout while acquiring depends on the channel set and interleave (measured 196-275 MB/s)."""
    return '%da%dd@%d' % (n_analog, n_digital, int(round(rate)))


def readout_get(key):
    return load().get('readout', {}).get(key)


def readout_get_any_rate(n_analog, n_digital, full_only=False):
    """Another rate of the same channel set, when this one has never been measured.

    The readout while acquiring depends on the channel set far more than on the sample rate (one analog channel
    measured 287 MB/s at 200 and at 400 MSa/s, two analog 287 at 100 and at 200), so a measurement of the same analog
    and digital counts is a better planning value than the static ceiling. The lowest cached value is returned: a set
    measured while the host was slow must not make a faster plan look optimistic.

    full_only: skip the runs where the host merely kept up (lower_bound). Those record the rate the run *produced*,
    not what the readout could do, so the number is a floor on the readout — fine to ask "can the host keep up", wrong
    to plan a duration with (4 analog + 8 digital at 50 MSa/s: a 166.7 MB/s floor gives 4.02 s gap-free, the
    207.4 MB/s the same set measured elsewhere gives 9.8 s).
    """
    prefix = '%da%dd@' % (n_analog, n_digital)
    best = None
    for key, val in (load().get('readout') or {}).items():
        if key.startswith(prefix) and isinstance(val, dict) and isinstance(val.get('mb_s'), (int, float)):
            if full_only and val.get('lower_bound'):
                continue
            if best is None or val['mb_s'] < best['mb_s']:
                best = dict(val, key=key)
    return best


def readout_put(key, mb_s, lower_bound, data_mb_s):
    d = load()
    d.setdefault('readout', {})[key] = {'mb_s': round(mb_s, 3), 'lower_bound': bool(lower_bound),
                                        'data_mb_s': round(data_mb_s, 1), 'time': time.strftime('%Y-%m-%dT%H:%M:%S')}
    save(d)
