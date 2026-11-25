#!/usr/bin/env python3
"""Readout ceiling on one bench: how many MB/s the host can read, per channel configuration.

Run by hand on a machine with the M454 attached (it is not part of pytest):

    python tests/hardware/research/usb_ceiling.py --bench-confirmed            # matrix + trace, writes the two CSVs
    python tests/hardware/research/usb_ceiling.py --bench-confirmed --quick    # matrix only

It drives the generator into the declared input, so it asks for the same per-run confirmation the hardware suite does
(--bench-confirmed): without it the script stops before opening the scope and lists the declared links.

What it measures, and why the columns are separate:
  logical_mb_s  rate x (analog + digital/8), the program's own number
  wire_mb_s     bytes actually received / signal time (acq.data_mb / acq.duration_s)
  host_mb_s     bytes received / wall time, i.e. what the host sustained
  backlog_mb    the loop ring's fill at the stop (logger.BacklogMeter)
A duration is enforced on the bytes read, so a run whose produced rate is above the readout keeps going until the
host has read the planned window: wire and host rates differ and the ring ends up holding (D - R) x wall time.
"""
import argparse
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

import usb1                                                        # noqa: E402

from openm454 import logger                                       # noqa: E402
from openm454 import scope as scopemod                           # noqa: E402
from tests.support import bench as benchmod                       # noqa: E402

CONFIGS = [
    ('1A 400M', ['A0'], [], 400e6),
    ('2A 200M', ['A0', 'A1'], [], 200e6),
    ('4A 100M', ['A0', 'A1', 'A2', 'A3'], [], 100e6),
    ('2A+2D 100M', ['A0', 'A1'], ['D0', 'D1'], 100e6),
    ('2A+8D 100M', ['A0', 'A1'], ['D%d' % i for i in range(8)], 100e6),
    ('4A+8D 50M', ['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)], 50e6),
    ('2D 400M', [], ['D0', 'D1'], 400e6),
    ('4D 200M', [], ['D0', 'D1', 'D2', 'D3'], 200e6),
]


def matrix(scope, seconds):
    rows = []
    for name, an, dg, rate in CONFIGS:
        st = scopemod.Settings()
        st.analog, st.digital, st.rate, st.duration = list(an), list(dg), rate, 1e-3
        pl = logger.plan(st, rate, seconds, every=1.0, out_dir=None, storage='none',
                         zero_info=scope.dev.zero_info, force=True)
        t0 = time.time()
        res = scope.record(st, rate, duration=seconds, every=1.0, storage='none', force=True)
        a = res['acq']
        wire = a['data_mb'] / a['duration_s'] if a['duration_s'] else 0.0
        rows.append({'config': name, 'logical_mb_s': round(pl.get('data_mb_s') or 0, 1),
                     'wire_mb_s': round(wire, 1), 'host_mb_s': round(a.get('host_mb_s') or 0, 1),
                     'backlog_mb': round(a.get('backlog_max_mb') or 0, 1),
                     'transfers': a.get('transfers'), 'wall_s': round(time.time() - t0, 2)})
        print('  %-12s logical %6.1f  wire %6.1f  host %6.1f  backlog %5.1f  (%.1f s)' % (
            name, rows[-1]['logical_mb_s'], rows[-1]['wire_mb_s'], rows[-1]['host_mb_s'],
            rows[-1]['backlog_mb'], rows[-1]['wall_s']), file=sys.stderr, flush=True)
    return rows


def transfers(scope, name='1A 400M', rate=400e6, seconds=1.0):
    """Every EP 0x86 completion, with the bytes and the reads still queued: the pacing evidence."""
    trace = []
    from openm454 import acquire
    orig = acquire.Engine._cb

    def patched(self, tr):
        try:
            st = tr.getStatus()
            n = tr.getActualLength()
            if tr is not getattr(self, '_head_tr', None) and st == usb1.TRANSFER_COMPLETED and n:
                trace.append((time.perf_counter(), n, sum(1 for t in self._trs if t.isSubmitted())))
        except Exception:                                          # noqa: BLE001 - a probe never breaks the run
            pass
        return orig(self, tr)

    acquire.Engine._cb = patched
    try:
        st = scopemod.Settings()
        st.analog, st.digital, st.rate, st.duration = ['A0'], [], rate, 1e-3
        scope.record(st, rate, duration=seconds, every=1.0, storage='none', force=True)
    finally:
        acquire.Engine._cb = orig
    return trace


def write_csv(path, rows):
    if not rows:
        return
    with open(path, 'w', encoding='utf-8') as f:
        f.write(','.join(rows[0]) + '\n')
        for r in rows:
            f.write(','.join(str(r[k]) for k in rows[0]) + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--seconds', type=float, default=1.2, help='planned window per configuration')
    ap.add_argument('--quick', action='store_true', help='skip the transfer trace')
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
    ceiling = os.path.join(args.out_dir, 'usb-ceiling.csv')
    transfers_path = os.path.join(args.out_dir, 'usb-transfers.csv')
    produced, crash = [], None
    restored, manifest_ok = True, True
    try:
        link = benchmod.Bench(benchmod.load()).link('awg_to_a0')
        link.program(scope)
        print('matrix:', file=sys.stderr)
        rows = matrix(scope, args.seconds)
        write_csv(ceiling, rows)
        produced.append(ceiling)                     # only a file this run wrote is listed in the manifest
        if not args.quick:
            print('transfer trace: 1 analog at 400 MSa/s, 1 s', file=sys.stderr)
            trace = transfers(scope)
            if trace:
                span = trace[-1][0] - trace[0][0]
                total = sum(n for _, n, _ in trace)
                gaps = [b[0] - a[0] for a, b in zip(trace, trace[1:])]
                print('  %d transfers, %.1f MB/s, gap p50 %.3f ms, inflight min/med/max %d/%d/%d' % (
                    len(trace), total / 1e6 / span, statistics.median(gaps) * 1e3,
                    min(t[2] for t in trace), statistics.median([t[2] for t in trace]),
                    max(t[2] for t in trace)), file=sys.stderr)
                t0 = trace[0][0]
                write_csv(transfers_path,
                          [{'t_s': round(t - t0, 6), 'bytes': n, 'inflight': i} for t, n, i in trace])
                produced.append(transfers_path)
    except BaseException as e:                       # a crash still leaves a manifest that says what happened
        crash = repr(e)
        raise
    finally:
        # The generator and the device are released before anything else can fail: a full disk must not leave the
        # bench driving a signal (the review caught the manifest write skipping the restore).
        try:
            extra = {'configs': [c[0] for c in CONFIGS], 'seconds': args.seconds}
            if crash:
                extra['error'] = crash
            manifest = benchmod.write_manifest(args.out_dir, scope, __file__, args, extra, files=produced,
                                              candidates=['usb-ceiling.csv', 'usb-transfers.csv'])
            if manifest['stale']:                    # an older run's file: say whose it is, never delete it
                print('%s not written this run: the file(s) there are from an earlier run'
                      % ', '.join(manifest['stale']), file=sys.stderr)
        except Exception as e:                       # noqa: BLE001 - reported, never fatal: the run's data is written
            manifest_ok = False
            print('the manifest could not be written (%r): %s' % (e, args.out_dir), file=sys.stderr)
        finally:
            restored = benchmod.restore_or_park(scope, before)
            scope.close()

    return 0 if (restored and manifest_ok) else 1


if __name__ == '__main__':
    raise SystemExit(main())
