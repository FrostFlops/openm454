#!/usr/bin/env python3
"""Signal quality against the channel configuration, on one bench (run by hand; not part of pytest).

    python tests/hardware/research/signal_quality.py --bench-confirmed --rounds 60

Every round captures each configuration once, in rotation, so a slow drift of the generator or the front end cannot be
mistaken for a configuration effect. Only the declared link's channel carries a signal (the built-in generator, here
0-0.20 V at 1 kHz so that 0.05 V/div gives 1 LSB = 1.7 mV); the other analog inputs are left open and the digital ones
unconnected — they still produce samples, which is what a configuration costs.

Per capture: flat-region sigma and amplitude, the even/odd and mod-4 group means (interleave mismatch), the 10-90 %
rise and the 50 % crossing position. Writes one row per capture plus a summary with t-based 95 % intervals.
"""
import argparse
import json
import math
import os
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

from openm454 import scope as scopemod                           # noqa: E402
from tests.support import bench as benchmod                       # noqa: E402

LSB_MV = 50.0 / 29.375            # 0.05 V/div, 8 bit
LEVEL_V = 0.10
SWING_V = 0.20

CONFIGS = [
    ('1A 400M', ['A0'], [], 400e6),
    ('1A 200M', ['A0'], [], 200e6),
    ('2A 200M', ['A0', 'A1'], [], 200e6),
    ('2A 100M', ['A0', 'A1'], [], 100e6),
    ('4A 100M', ['A0', 'A1', 'A2', 'A3'], [], 100e6),
    ('4A+8D 100M', ['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)], 100e6),
]


def one(scope, an, dg, rate):
    st = scopemod.Settings()
    st.analog, st.digital, st.rate, st.duration = list(an), list(dg), rate, 10e-3
    for ch in ('A0', 'A1', 'A2', 'A3'):
        st.vdiv[ch], st.offset[ch] = 0.05, LEVEL_V
    st.trig_source, st.trig_edge, st.trig_level, st.trig_mode = 'A0', 'rise', LEVEL_V, 'normal'
    st.timeout = 2.0
    cap = scope.capture(st)
    applied = cap.meta['acq']['analog_rate_hz']
    v = cap.volts('A0')
    dt = 1.0 / applied
    k = int(round(-cap.meta['t0_analog_s'] * applied))
    hi, lo = v[k + 2000:k + 20000], v[k - 20000:k - 2000]
    amp = float(hi.mean() - lo.mean())
    a10, a90 = lo.mean() + 0.1 * amp, lo.mean() + 0.9 * amp
    j = k - 200 + int(np.argmax(v[k - 200:] > a10))
    k90 = j + int(np.argmax(v[j:j + 400] > a90))
    k50 = k - 200 + int(np.argmax(v[k - 200:] > lo.mean() + 0.5 * amp))
    return {'rate_hz': int(applied), 'sigma_mv': round(max(float(hi.std()), float(lo.std())) * 1e3, 3),
            'amp_mv': round(amp * 1e3, 3),
            'evenodd_mv': round(abs(float(hi[0::2].mean() - hi[1::2].mean())) * 1e3, 3),
            'mod4_mv': round(float(max(hi[i::4].mean() for i in range(4)) -
                                   min(hi[i::4].mean() for i in range(4))) * 1e3, 3),
            'rise_ns': round((k90 - j) * dt * 1e9, 1), 'edge_ns': round((k50 - k) * dt * 1e9, 2)}


def median_ci(values, draws=2000, seed=1):
    """Median and a bootstrap 95 % interval. The median, not the mean: a transient disturbance of the bench shows up
    as a few records with several times the usual sigma (6 of 360 in the run this file ships with), which a mean
    would smear over the whole configuration."""
    xs = np.asarray(values, dtype=float)
    med = float(np.median(xs))
    rng = np.random.default_rng(seed)
    meds = np.median(rng.choice(xs, size=(draws, xs.size)), axis=1)
    mad = float(np.median(np.abs(xs - med))) * 1.4826      # robust scale: a transient is many MADs away
    return {'median': round(med, 3), 'lo': round(float(np.percentile(meds, 2.5)), 3),
            'hi': round(float(np.percentile(meds, 97.5)), 3), 'n': int(xs.size),
            'transients': int(np.count_nonzero(np.abs(xs - med) > 3 * mad)) if mad > 0 else 0}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--rounds', type=int, default=60)
    ap.add_argument('--out-dir', default=HERE)
    ap.add_argument('--bench-confirmed', action='store_true',
                    help='the bench owner confirmed for this run that the bench is wired exactly as '
                         'tests/hardware/bench.json declares (the script drives the generator into it)')
    args = ap.parse_args(argv)
    if not args.bench_confirmed:                     # before the scope is opened: nothing is driven unconfirmed
        raise SystemExit(benchmod.wiring_gate_message('this research script'))
    os.makedirs(args.out_dir, exist_ok=True)         # a first run into a new directory must not fail after the run

    scope = scopemod.Scope()
    scope.open()
    before = scope.awg_status()                  # the generator is write-only: restore what we found
    path = os.path.join(args.out_dir, 'signal-quality.csv')
    keys = ['round', 'config', 'rate_hz', 'sigma_mv', 'amp_mv', 'evenodd_mv', 'mod4_mv', 'rise_ns', 'edge_ns']
    rows, produced, crash = [], [], None
    restored, manifest_ok = True, True
    try:
        link = benchmod.Bench(benchmod.load()).link('awg_to_a0')
        scope.awg_set('square', 1e3, SWING_V, 0.0)
        for r in range(args.rounds):
            order = CONFIGS[r % len(CONFIGS):] + CONFIGS[:r % len(CONFIGS)]
            for name, an, dg, rate in order:
                row = one(scope, an, dg, rate)
                row.update({'round': r, 'config': name})
                rows.append(row)
            if (r + 1) % 10 == 0:
                print('  %d/%d rounds' % (r + 1, args.rounds), file=sys.stderr, flush=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(','.join(keys) + '\n')
            for row in rows:
                f.write(','.join(str(row[k]) for k in keys) + '\n')
        produced.append(path)                        # listed only once the file is really there
    except BaseException as e:                       # a failed capture still leaves a manifest that says so
        crash = repr(e)
        raise
    finally:
        # The generator and the device are released before anything else can fail: a full disk must not leave the
        # bench driving a signal (the review caught the manifest write skipping the restore).
        try:
            extra = {'configs': [c[0] for c in CONFIGS], 'swing_v': SWING_V, 'vdiv_v': 0.05, 'link': 'awg_to_a0'}
            if crash:
                extra['error'] = crash
            manifest = benchmod.write_manifest(args.out_dir, scope, __file__, args, extra, files=produced,
                                              candidates=['signal-quality.csv'])
            if manifest['stale']:
                print('%s not written this run: the file(s) there are from an earlier run'
                      % ', '.join(manifest['stale']), file=sys.stderr)
        except Exception as e:                       # noqa: BLE001 - reported, never fatal: the run's data is written
            manifest_ok = False
            print('the manifest could not be written (%r): %s' % (e, args.out_dir), file=sys.stderr)
        finally:
            restored = benchmod.restore_or_park(scope, before)
            scope.close()

    summary = {}
    for name, *_ in CONFIGS:
        mine = [r for r in rows if r['config'] == name]
        summary[name] = {k: median_ci([r[k] for r in mine]) for k in ('sigma_mv', 'amp_mv', 'evenodd_mv', 'mod4_mv')}
        summary[name]['rise_ns'] = statistics.median([r['rise_ns'] for r in mine])
        summary[name]['rate_hz'] = mine[0]['rate_hz']
    # paired differences: every round holds every configuration, so a difference taken inside a round cannot be
    # moved by the session drift that shows between runs (the absolute medians moved up to 20 % between two runs of
    # this same script, the same order as the effect being measured)
    pairs = [('1A 200M', '2A 200M'), ('2A 100M', '4A 100M'), ('4A 100M', '4A+8D 100M'), ('1A 200M', '1A 400M')]
    rounds = sorted({r['round'] for r in rows})
    diffs = {}
    for a, b in pairs:
        da = {r['round']: r for r in rows if r['config'] == a}
        db = {r['round']: r for r in rows if r['config'] == b}
        key = '%s -> %s' % (a, b)
        diffs[key] = {}
        for field in ('sigma_mv', 'amp_mv'):
            deltas = [float(db[r][field]) - float(da[r][field]) for r in rounds if r in da and r in db]
            diffs[key][field] = median_ci(deltas)
    print(json.dumps({'configs': summary, 'paired': diffs}, indent=1))
    return 0 if (restored and manifest_ok) else 1


if __name__ == '__main__':
    raise SystemExit(main())
