"""Endless, gap-free logging over loop mode (HW_BASE_MODE 6, DSWave's Data Acquisition mode).

What the hardware does (measured 2026-11-01/02, docs/PROTOCOL.md section 8.1):
  * after the head the device streams plain sample blocks until it is stopped; nothing in the stream marks time,
    so a loss could only be seen in the signal;
  * the 512 MiB sample memory is a ring: the host may fall behind by up to ~512 MiB without loss; past that the
    device overwrites unread data without any flag and keeps streaming, misaligned (garbage) for the rest of the
    run; the break came at 508-511 MiB (2026-11-01, the older estimate) and ~514 MiB (2026-11-02, BacklogMeter);
  * readout while acquiring: 196-275 MB/s depending on the channel set; 150 MB/s ran 3 min (27 GB) with a constant
    host lag;
  * nothing is read out after a stop: a run must keep acquiring until the host has read everything it wants, so
    above the readout limit R the backlog grows for the whole run: gap-free for at most gapfree_max_s;
  * one rate for every channel, 100 MHz / N for any integer N; above 100 MSa/s the converters are interleaved as in a normal
    capture (DSWave never does this in loop mode; verified on hardware 2026-11-02): 200 MSa/s with <= 2 analog and
    <= 4 digital channels, 400 MSa/s with <= 1 analog and <= 2 digital channels.

So the host decides everything. plan() checks that the rate is sustainable with the readout measured for this
channel set (measure_readout, cached in state/host.json); above it a log is gap-free for gapfree_max_s only, and a
longer one is compared with `stream` (records of <= 512 MiB sent while acquired, short gaps): refused with
STREAM_BETTER when stream's records are at least as long as the log's single gap-free piece, else NEEDS_FORCE
(--force records that piece and stops: 'ring_full'). It also works out where the data goes. run() streams with the engine keeping nothing in memory: the USB loop only counts and queues the raw transfers,
a writer thread decodes them, prints the live statistics and keeps or writes the samples, and the backlog (data the
device has acquired but the host has not read) is watched so a run stops well before the ring could overwrite data.

Storage (most logs are short: RAM is the default, an SSD written at 100+ MB/s for hours wears out):
  'ram'  (default) the samples stay in RAM within the budget (state/host.json allowance, else min(4 GB, a quarter of
         the free RAM)) and are written once, at the end, as <stem>.npz. A log longer than the budget is refused
         unless keep_last (or 'forever'): then the RAM is a ring and the newest budget's worth is kept.
  'disk' a directory <stem>/ with chunk_00000.npz ... and index.json, dumped while running (for records that must be
         kept in full); chunk k starts at t0 = k x chunk_s.
  'none' statistics only.
Every .npz is a normal capture file (load_capture / analyze / plot); t = 0 is the first sample of the log.
"""
import collections
import json
import math
import os
import queue
import shutil
import threading
import time
import zipfile

import numpy as np

from . import packet
from .errors import DscopeError

RING_BYTES = 512 * 2 ** 20           # device sample memory = loop ring
BACKLOG_STOP = 480 * 2 ** 20         # stop a run once the device is this far ahead: the ring overwrote unread data at
                                     # 508-514 MiB of the estimate (hardware, 2026-11-01/02); the margin covers the
                                     # drift band and the in-flight reads
SUSTAINED_BPS = 150e6                # verified: 3 min, 27 GB, host lag constant
CEILING_BPS = 190e6                  # planning ceiling; the readout while acquiring measured 196-275 MB/s
MAX_RATE = 100_000_000               # one converter per channel (fold 0, DSWave's loop mode)
FOLD_RATES = (200_000_000, 400_000_000)  # interleaved loop rates (loop_fold)
MAX_DIVIDER = 100_000                # 100 MHz / N for any integer N up to this (1 kSa/s; all verified as dividers)


def max_rate(n_analog, n_digital):
    """Highest log rate for a channel set: 100 MSa/s x the interleave factor both domains allow (analog 4/2/1 for
    1/2/3-4 channels, digital 4/2/1 for 1-2/3-4/5-8 channels, as in a normal capture)."""
    fa = {0: 4, 1: 4, 2: 2}.get(n_analog, 1)
    fd = 4 if n_digital <= 2 else (2 if n_digital <= 4 else 1)
    return MAX_RATE * min(fa, fd)
LOOP_DEPTH = 400_000                 # :STORAGEdepth sent with the loop program (DSWave keeps the last OSC value)
CHUNK_TARGET_BYTES = 256e6           # default size of one dumped chunk file
QUEUE_LIMIT_BYTES = 768 * 2 ** 20    # raw data waiting for the writer thread; more means disk or CPU are too slow
PIECE_BYTES = 32 * 2 ** 20           # the USB loop hands the writer pieces of at most this (statistics lines are
                                     # assembled from pieces, so --every does not size the queue)
CHUNK_MAX_BYTES = 2 ** 30            # one disk chunk is collected in RAM before it is written
CLOCK_TOL = 200e-6                   # scope vs PC clock: the backlog baseline may rise this fast (x the data rate),
                                     # so a scope clock up to 200 ppm fast never looks like a growing backlog
DRIFT_BAND = 16 * 2 ** 20            # ... but only while the estimate is below this (the host caught up): a real
                                     # backlog is never absorbed beyond it
RAM_BUDGET_MAX = 4 * 2 ** 30         # default RAM budget: min(this, a quarter of the free memory)
DISK_WEAR_BYTES = 100e9              # warn when a disk log writes more than this (or this per day when unbounded)
STORAGES = ('ram', 'disk', 'none')
RESTART_S = 0.18                     # measured: stop handshake, 100 ms quiet, setup, 50 ms before the next start
RECORD_SAMPLES = 400_000_000         # a stream record: at most 400M samples per channel and 512 MiB
PROBE_S = 2.0                        # readout probe before a log above SUSTAINED_BPS whose channel set was not measured
PROBE_BACKLOG = 256 * 2 ** 20        # ... ends early once the scope is this far ahead (its data are dropped anyway)
KEPT_UP = 0.99                       # a probe reading >= this x D: the host keeps up (R >= D, a lower bound)
SMALL_PKT_TRANSFERS = 4              # small packets: DSWave queues 1 read of one block, the driver at least this many


def available_ram():
    """Free physical memory in bytes (best effort; 8 GiB when it cannot be read)."""
    try:
        if os.name == 'nt':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys)
        return int(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
    except (OSError, ValueError, AttributeError):
        return 8 * 2 ** 30


def default_ram_budget():
    """The owner's allowance (openm454 bench --ram), capped by 80 % of the free RAM; else min(4 GB, free / 4)."""
    from . import host
    free = available_ram()
    allowed = host.allow().get('ram_bytes')
    if allowed:
        return int(min(allowed, 0.8 * free))
    return int(min(RAM_BUDGET_MAX, free // 4))


def planned_transfers(geom):
    """EP 0x86 reads a loop run asks the engine to queue at once (on Linux the engine may queue fewer when the usbfs
    budget cannot hold them: the run's acq.transfers is the depth actually used)."""
    n = geom['transfer_count']
    return max(SMALL_PKT_TRANSFERS, n) if geom['small_pkt'] else n


def loop_hal(st, rate, fold=False, divider=None):
    """HAL parameters of a loop run: the front-end settings of `st`, trigger off, one explicit rate; fold:
    interleave the converters (rates above 100 MSa/s)."""
    from .scope import hal_config
    import copy
    s = copy.copy(st)
    s.duration, s.rate, s.trig_source, s.trig_mode = 1.0, None, None, 'force'
    hal, scale, warnings, _ = hal_config(s)
    hal['trigger']['enable'] = 0
    hal.update(loop=1, loop_fold=int(bool(fold)), rate=rate, divider=divider, auto_rate=0, storage_depth=LOOP_DEPTH,
               sample_time_ps=10 ** 12)
    return hal, scale, [w for w in warnings if 'samples' not in w and 'window' not in w]


def plan(st, rate, duration=None, every=1.0, out_dir=None, storage='ram', ram_budget=None, chunk_s=None,
         zero_info=None, force=False, keep_last=False, disk_budget=None, defer=False, remeasure=False):
    """Offline plan of a loop log. duration None = until stopped. Raises INVALID_ARG for what cannot work.
    The readout while acquiring (R) depends on the channel set: it comes from state/host.json (measured by a probe
    or an earlier run), else the planning ceiling of 190 MB/s is assumed. Above R the scope's memory fills at D - R
    and nothing can be read after the stop, so a log is gap-free for at most gapfree_max_s. A longer one is compared
    with `stream` (stream_alternative): STREAM_BETTER when stream's records are at least as long as that single
    piece (or one record holds the whole duration), else NEEDS_FORCE: log gives the longest gap-free piece, and
    force records it and stops when the scope is BACKLOG_STOP ahead (stop_reason 'ring_full'). Both errors carry
    the numbers in .plan. defer=True: with an assumed R nothing is refused (the run measures R first);
    remeasure: ignore the cached R (with defer: measure it again before deciding).
    keep_last: a RAM log longer than the budget keeps the newest budget's worth (ring) instead of being refused.
    disk_budget: bytes a disk log may use (default: the owner's allowance in state/host.json, else 90 % of free)."""
    from .scope import _cached_zero
    from . import mso_adapter, host
    na, nd = len(st.analog), len(st.digital)
    if not na and not nd:
        raise DscopeError('INVALID_ARG', 'no channels enabled')
    if storage not in STORAGES:
        raise DscopeError('INVALID_ARG', '--storage must be one of %s' % ', '.join(STORAGES))
    if not rate or rate <= 0:
        raise DscopeError('INVALID_ARG', '--rate must be > 0')
    warnings = []
    top = max_rate(na, nd)
    if rate > top * (1 + 1e-9):
        raise DscopeError('INVALID_ARG', 'log samples every channel at one rate: at most %d MSa/s for %d analog + %d '
                          'digital channels' % (top // 10 ** 6, na, nd),
                          'above 100 MSa/s the converters are interleaved: 200 MSa/s needs <= 2 analog and <= 4 '
                          'digital channels, 400 MSa/s <= 1 analog and <= 2 digital channels')
    divider = None
    if rate > MAX_RATE * (1 + 1e-9):                 # interleaved: 200 or 400 MSa/s
        r = max([x for x in FOLD_RATES if x <= rate * (1 + 1e-9) and x <= top])
    else:                                            # 100 MHz / N, any integer N (the FPGA's divider)
        divider = max(1, int(round(MAX_RATE / rate)))
        if divider > MAX_DIVIDER:
            raise DscopeError('INVALID_ARG', 'the lowest log rate is %d Sa/s' % (MAX_RATE // MAX_DIVIDER))
        r = MAX_RATE / divider
    if abs(r - rate) > 1e-6 * rate:
        warnings.append('rate set to %.7g Sa/s (%s): the sample clock is 100 MHz divided by an integer'
                        % (r, '100 MHz / %d' % divider if divider else 'interleaved'))
    if duration is not None and duration <= 0:
        raise DscopeError('INVALID_ARG', '--duration must be > 0 (or "forever")')
    if every <= 0:
        raise DscopeError('INVALID_ARG', '--every must be > 0')
    fold = r > MAX_RATE
    hal, scale, fe_warnings = loop_hal(st, r, fold, divider)
    warnings += fe_warnings
    prog, geom = mso_adapter.compile(hal, zero_info if zero_info is not None else _cached_zero())
    blk = geom['blk_bytes']
    small = bool(geom['small_pkt'])
    data_bps = r * (na + nd / 8.0)                       # device bytes per second = bytes kept per second
    usb_bps = data_bps * (packet.HEAD_BYTES / blk) if small else data_bps
    block_s = packet.BLOCK_SAMPLES / r
    every_blocks = max(1, int(round(every / block_s)))
    every = every_blocks * block_s
    pkt_s = (geom['pkt_bytes'] / usb_bps) if not small else block_s
    hb = host.load().get('bench', {})

    # readout while acquiring for this channel set: measured (probe / bench, cached) or the planning ceiling
    rkey = host.readout_key(na, nd, r)
    cached = None if remeasure else host.readout_get(rkey)
    if cached:
        R = cached['mb_s'] * 1e6
        rsrc = 'measured %s%s' % (cached['time'], ' (lower bound: the host kept up)' if cached['lower_bound'] else '')
    else:
        R, rsrc = CEILING_BPS, 'assumed (planning ceiling)'
    needs_probe = not cached and data_bps > SUSTAINED_BPS
    if needs_probe and defer:
        rsrc += '; a probe of up to %.0f s measures it when the log starts (the plan may change)' % PROBE_S
    kept_up = bool(cached and cached['lower_bound'] and data_bps <= R * (1 + 1e-3))    # stored rounded
    if data_bps <= SUSTAINED_BPS or kept_up or (cached and data_bps <= 0.95 * R):
        verdict = 'sustained'
    elif data_bps <= R:
        verdict = 'near_limit'
        warnings.append('%.0f MB/s is near the readout limit of this channel set (%.0f MB/s, %s); the backlog is '
                        'watched and the run stops before data could be lost' % (data_bps / 1e6, R / 1e6, rsrc))
    else:
        verdict = 'burst'
    # Above R the backlog grows at D - R, and the scope sends nothing after a stop: a log of T s ends when the host
    # has read D x T at R, at t = D x T / R, with a backlog of (D - R) x D x T / R that must stay below BACKLOG_STOP.
    burst_s = BACKLOG_STOP * R / (data_bps * (data_bps - R)) if verdict == 'burst' else None
    gapfree = None if burst_s is None else math.floor(burst_s * 100) / 100     # reported: never above the limit
    alt = None
    too_long = burst_s is not None and (duration is None or duration > burst_s)
    # log gives one gap-free piece of burst_s; stream covers the whole time in records with gaps. Stream is better
    # when it samples every channel at least as fast and its records are at least that long (or one holds it all).
    recommendation = 'log'
    if too_long:
        alt = stream_alternative(st, r, data_bps, R, duration)
        better = alt['same_rates'] and (alt['records'] == 1 or alt['record_s'] >= burst_s)
        recommendation = 'stream' if better else 'log --force'
    what = 'forever' if duration is None else '%g s' % duration
    if too_long and not force and not (defer and needs_probe):
        why = ('%s at %.0f MB/s: log is gap-free for the first %.2f s only (readout %.0f MB/s, %s: the scope gets '
               '%.0f MB/s ahead, and nothing can be read after a stop)' % (what, data_bps / 1e6, gapfree, R / 1e6, rsrc,
                                                                           (data_bps - R) / 1e6))
        if recommendation == 'stream':
            e = DscopeError('STREAM_BETTER', why + '; stream does better: %s' % alt['summary'],
                            'run: %s (or --duration <= %.2fs, a lower --rate, fewer channels; --force logs the %.2f s '
                            'anyway)' % (alt['command'], gapfree, gapfree))
        else:
            e = DscopeError('NEEDS_FORCE', why + '; that one piece is the longest gap-free recording possible (stream: '
                            '%s)' % alt['summary'],
                            'add --force to log the first ~%.2f s (the run then stops: ring_full; the rest of the %s '
                            'is not recorded)%s' % (gapfree, what, ', or use stream for the whole time with gaps: %s'
                                                    % alt['command'] if alt.get('command') else ''))
        e.plan = {'data_mb_s': round(data_bps / 1e6, 3), 'readout_mb_s': round(R / 1e6, 1), 'readout_source': rsrc,
                  'backlog_growth_mb_s': round((data_bps - R) / 1e6, 1), 'gapfree_max_s': gapfree,
                  'duration_s': duration, 'recommendation': recommendation, 'stream_alternative': alt}
        raise e
    if burst_s is not None:
        warnings.append('burst: the scope gets %.0f MB/s ahead (readout %.0f MB/s, %s): gap-free for at most %.2f s%s'
                        % ((data_bps - R) / 1e6, R / 1e6, rsrc, gapfree,
                           '; --force: the run stops when the scope is %.0f MiB ahead (ring_full)'
                           % (BACKLOG_STOP / 2 ** 20) if too_long else ''))
    if too_long and recommendation == 'stream':
        warnings.append('stream would do better: %s' % alt['summary'])
    if hb.get('decode_mb_s') and data_bps > 0.8e6 * hb['decode_mb_s'] and storage != 'none':
        warnings.append('%.0f MB/s is close to what this PC decodes (%.0f MB/s, openm454 bench): the run may stop with '
                        'HOST_TOO_SLOW' % (data_bps / 1e6, hb['decode_mb_s']))

    if ram_budget is None:
        ram_budget = default_ram_budget()
    run_s = duration
    if too_long and duration is not None:            # forced: stops at the guard (R may be a little better)
        run_s = min(duration, 1.25 * burst_s)
    total = None if run_s is None else data_bps * run_s
    ram_window = ram_budget / data_bps
    ring = False
    disk_bytes = None
    file_bytes = None
    max_s = burst_s
    disk_free = None
    d = out_dir or '.'
    if storage == 'ram':
        if total is None or total > ram_budget:
            if total is not None and not keep_last:
                raise DscopeError('INVALID_ARG', '%.2f GB (%.0f s at %.1f MB/s) does not fit the RAM budget of %.2f GB'
                                  % (total / 1e9, run_s, data_bps / 1e6, ram_budget / 1e9),
                                  '--keep-last keeps the newest %.0f s; --storage disk writes everything (it wears an SSD: '
                                  'only for records needed in full); --ram raises the budget; or a lower --rate / '
                                  'fewer channels' % ram_window)
            ring = True
            warnings.append('RAM ring: keeps the newest %.0f s (%.2f GB budget), written once at the end'
                            % (ram_window, ram_budget / 1e9))
        file_bytes = ram_budget if ring else total
        if out_dir is not None:
            os.makedirs(d, exist_ok=True)
            disk_free = shutil.disk_usage(d).free
            if file_bytes > 0.9 * disk_free:
                raise DscopeError('INVALID_ARG', 'not enough disk space in %s for the final file: %.2f GB free, %.2f GB '
                                  'needed' % (d, disk_free / 1e9, file_bytes / 1e9),
                                  'free space, another --out, a smaller --ram or a lower --rate')
    elif storage == 'disk':
        os.makedirs(d, exist_ok=True)
        disk_free = shutil.disk_usage(d).free
        allowed = disk_budget if disk_budget is not None else host.allow().get('disk_bytes')
        budget = min(0.9 * disk_free, allowed) if allowed else 0.9 * disk_free
        disk_bytes = budget
        disk_s = budget / data_bps
        max_s = disk_s if max_s is None else min(max_s, disk_s)
        if run_s is not None and run_s > disk_s:
            raise DscopeError('INVALID_ARG', 'not enough disk allowance in %s: %.1f GB hold %.0f s at %.1f MB/s'
                              % (d, budget / 1e9, disk_s, data_bps / 1e6),
                              'use RAM (--storage ram, --keep-last), a lower --rate, fewer channels, or raise the '
                              'allowance with openm454 bench --disk')
        per_day = data_bps * 86400
        if (total if total is not None else per_day) > DISK_WEAR_BYTES:
            warnings.append('disk log writes %s to %s (%.0f GB per hour); SSDs are rated for a few hundred TB written: '
                            'use RAM unless the whole record is needed' % (
                                '%.0f GB' % (total / 1e9) if total is not None else 'until stopped', d,
                                data_bps * 3600 / 1e9))
        if hb.get('disk_write_mb_s') and hb.get('disk_dir') and os.path.abspath(hb['disk_dir']) == os.path.abspath(d) \
                and data_bps > 0.7e6 * hb['disk_write_mb_s']:
            warnings.append('%.0f MB/s is close to the measured write speed of %s (%.0f MB/s)'
                            % (data_bps / 1e6, d, hb['disk_write_mb_s']))
    if chunk_s is None:
        chunk_s = min(max(CHUNK_TARGET_BYTES / data_bps, every), 3600.0)
    chunk_blocks = max(every_blocks, int(round(chunk_s / every)) * every_blocks)
    if storage == 'disk' and chunk_blocks * blk > CHUNK_MAX_BYTES:
        chunk_blocks = max(1, CHUNK_MAX_BYTES // blk)
        warnings.append('chunk limited to %.0f MB (%.3g s): a chunk is collected in RAM before it is written'
                        % (chunk_blocks * blk / 1e6, chunk_blocks * block_s))
    chunk_s = chunk_blocks * block_s
    if storage == 'ram' and not ring and run_s is not None:
        max_s = run_s if max_s is None else min(max_s, run_s)
    pub = {
        'rate_hz': r, 'channels': list(st.analog) + list(st.digital), 'data_mb_s': round(data_bps / 1e6, 3),
        'usb_mb_s': round(usb_bps / 1e6, 3), 'verdict': verdict,
        'sustained_mb_s': SUSTAINED_BPS / 1e6, 'ceiling_mb_s': CEILING_BPS / 1e6,
        'duration_s': duration, 'ring_s': round(RING_BYTES / data_bps, 3), 'storage': storage,
        'ram_budget_mb': round(ram_budget / 1e6), 'ram_window_s': round(ram_window, 3), 'ram_ring': ring,
        'file_mb': None if file_bytes is None else round(file_bytes / 1e6, 1),
        'total_mb': None if total is None else round(total / 1e6, 1),
        'chunk_s': round(chunk_s, 6) if storage == 'disk' else None,
        'chunk_mb': round(chunk_blocks * blk / 1e6, 1) if storage == 'disk' else None,
        'chunks': (math.ceil(run_s / chunk_s) if run_s else None) if storage == 'disk' else None,
        'disk_free_gb': None if disk_free is None else round(disk_free / 1e9, 1),
        'max_duration_s': None if max_s is None else math.floor(max_s * 100) / 100, 'every_s': round(every, 6),
        'small_packets': small, 'packet_bytes': geom['pkt_bytes'], 'transfers_planned': planned_transfers(geom),
        'interleaved': fold, 'divider': divider, 'max_rate_hz': top,
        'readout_mb_s': round(R / 1e6, 1), 'readout_source': rsrc,
        'backlog_growth_mb_s': round(max(0.0, data_bps - R) / 1e6, 1), 'gapfree_max_s': gapfree,
        'backlog_stop_mb': round(BACKLOG_STOP / 1e6, 1), 'recommendation': recommendation,
        'stream_alternative': alt, 'provisional': bool(defer and needs_probe), 'warnings': warnings}
    return dict(pub, _hal=hal, _scale=scale, _prog=prog, _geom=geom, _every_blocks=every_blocks,
                _chunk_blocks=chunk_blocks, _data_bps=data_bps, _pkt_s=pkt_s, _ram_budget=ram_budget,
                _disk_bytes=disk_bytes, _needs_probe=needs_probe, _readout_key=rkey, _force=bool(force and too_long),
                _ram_limit=(min(ram_budget, 0.9 * disk_free) if disk_free is not None else ram_budget)
                if storage == 'ram' and not ring else None)


def _rate_arg(rate):
    return '%gM' % (rate / 1e6) if rate >= 1e6 else ('%gk' % (rate / 1e3) if rate >= 1e3 else '%g' % rate)


def stream_alternative(st, rate, data_bps, readout, duration):
    """What `stream` gives instead of a log above the readout, from stream's own planning (scope.stream_record_plan):
    the lead domain runs at fold x the smallest 1-2-5 table rate that is >= `rate`, the other domain follows, and a
    record holds <= 400M samples per channel and <= 512 MiB. Each record is sent while acquired and ends with a drain
    of record x (D_stream - R) / R plus the ~0.18 s restart (R: this log's readout; stream's interleave can make it
    lower). same_rates: every channel at >= `rate` (otherwise stream is no substitute)."""
    import copy
    from . import mso
    from .scope import Scope, stream_record_plan, vertical_flags, afold_for, dfold_for
    na, nd = len(st.analog), len(st.digital)
    lead = afold_for(na) if na else dfold_for(nd)
    steps = sorted(t for t in mso.SAMPLERATES if t * lead >= rate * (1 - 1e-9))
    s = copy.copy(st)
    s.rate = lead * steps[0] if steps else rate
    s.duration = duration if duration is not None else 1.0
    s.trig_source, s.trig_mode = None, 'force'
    what = 'run' if duration is None else '%g s' % duration
    try:
        rec, rec_s, chained = stream_record_plan(s, duration, s.rate)
        g = Scope().plan(rec)['geometry']
    except DscopeError as e:
        return {'same_rates': False, 'records': None, 'record_s': None, 'gap_s': None, 'coverage_pct': None,
                'command': None, 'summary': 'stream cannot record these channels at %s (%s)' % (_rate_arg(rate),
                                                                                             e.message)}
    ar = g.get('analog_rate') if na else None
    dr = g.get('digital_rate') if nd else None
    slower = ['%s at %s' % (k, _rate_arg(x)) for k, x in (('analog', ar), ('digital', dr))
              if x is not None and x < rate * (1 - 1e-6)]
    d_stream = (ar or 0) * na + (dr or 0) * nd / 8.0
    cmd = ('python -m openm454 stream -c %s --rate %s --duration %s %s' % (
        ','.join(list(st.analog) + list(st.digital)), _rate_arg(s.rate),
        'forever' if duration is None else '%gs' % duration, vertical_flags(st))).strip()
    out = {'analog_rate_hz': ar, 'digital_rate_hz': dr, 'same_rates': not slower, 'command': cmd,
           'note': 'gaps estimated with this log\'s readout; with interleaved converters a stream can read out slower'}
    if not chained:
        out.update(record_s=round(rec_s, 3), gap_s=0.0, coverage_pct=100.0, records=1,
                   summary='one record holds all %g s gap-free' % duration)
    else:
        gap = rec_s * max(0.0, d_stream - readout) / readout + RESTART_S
        cov = 100 * rec_s / (rec_s + gap)
        out.update(record_s=round(rec_s, 3), gap_s=round(gap, 3), coverage_pct=round(cov, 1),
                   records=None if duration is None else int(math.ceil(duration / (rec_s + gap))),
                   summary='records of %.2f s with %.2f s gaps over the whole %s (%.0f %% of the signal)'
                   % (rec_s, gap, what, cov))
    if slower:
        out['summary'] += ', but it samples %s only' % ' and '.join(slower)
    return out


def measure_readout(scope, pl, seconds=PROBE_S):
    """Run the planned loop program with nothing kept and measure how fast this PC reads it while the scope acquires
    (it depends on the channel set): for `seconds`, or until the scope is PROBE_BACKLOG ahead. The rate over the
    last two thirds decides: >= KEPT_UP x D = the host kept up (stored as the lower bound R = D), else R itself.
    Cached in state/host.json. -> (R in bytes/s, kept_up)."""
    from . import host
    geom = dict(pl['_geom'])
    small = bool(geom['small_pkt'])
    geom['transfer_count'] = planned_transfers(geom)
    blk = packet.layout_from_regs(geom['_regs'], rates={'analog': pl['rate_hz'], 'digital': pl['rate_hz']}).block_bytes
    D = pl['_data_bps']
    log = []
    p = {'meter': None, 'n': 0}

    def on_chunk(b):
        now = time.perf_counter()
        if p['meter'] is None:
            p['meter'] = BacklogMeter(D, now)
            return
        p['n'] += blk if small else len(b)
        log.append((now, p['n']))

    def stop():
        m = p['meter']
        if m is None:
            return False
        now = time.perf_counter()
        return now - m.t0 > seconds or m.update(now, p['n']) > PROBE_BACKLOG
    scope._run(pl['_prog'], geom, 3.0, None, on_chunk=on_chunk, keep_data=False, poll=False, stop_flag=stop)
    if len(log) < 6:
        raise DscopeError('PROTOCOL_ERROR', 'readout probe: no data from the scope')
    (t0, n0), (t1, n1) = log[len(log) // 3], log[-1]    # skip the start (the queued reads fill first)
    R = (n1 - n0) / (t1 - t0) if t1 > t0 else D
    kept_up = R >= KEPT_UP * D
    if kept_up:
        R = D
    host.readout_put(pl['_readout_key'], R / 1e6, kept_up, D / 1e6)
    return R, kept_up


def public(pl):
    return {k: v for k, v in pl.items() if not k.startswith('_')}


def write_npz(path, meta, members):
    """np.savez-compatible file written member by member from lists of uint8 parts (no concatenated copy).
    members: [(name, [parts...]), ...]."""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, parts in [('meta', [np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)])] + list(members):
            n = sum(int(p.size) for p in parts)
            with zf.open(name + '.npy', 'w', force_zip64=True) as f:
                np.lib.format.write_array_header_2_0(f, {'descr': '|u1', 'fortran_order': False, 'shape': (n,)})
                for p in parts:
                    f.write(memoryview(np.ascontiguousarray(p, dtype=np.uint8)))
    return path


class _Writer(threading.Thread):
    """Decodes queued raw blocks, prints a statistics line per block, keeps (RAM) or dumps (disk) the samples."""

    def __init__(self, layout, rate, scale, storage, chunk_blocks, stem, meta_base, emit, heartbeat,
                 ram_budget=None, ring=False, every_blocks=1, hook=None):
        super().__init__(daemon=True)
        self.hook = hook                  # openm454.hook.Hook: user code called with every piece
        self.live_s = None                # callable: signal time of the newest sample the scope has taken
        self.t0_wall = None               # time.time() of the run start (t_wall_s)
        self.layout, self.rate, self.scale = layout, rate, scale
        self.storage, self.chunk_blocks, self.stem = storage, chunk_blocks, stem
        self.meta_base, self.emit, self.heartbeat = meta_base, emit, heartbeat
        self.ram_budget, self.ring = ram_budget, ring
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.queued = 0
        self.error = None
        self.carry = b''
        self.blocks = 0                   # blocks processed
        self.line = 0
        self.an = ['A%d' % i for i in layout.analog]
        self.dg = ['D%d' % i for i in layout.digital]
        self.every_blocks = every_blocks
        self.lacc, self.line_blocks, self.line_info = {}, 0, {}    # the statistics line being assembled
        self.acc = {c: {'min': 255, 'max': 0, 'sum': 0, 'n': 0} for c in self.an}
        self.dacc = {c: {'ones': 0, 'edges': 0, 'n': 0, 'last': None} for c in self.dg}
        self.cur = {c: [] for c in self.an + self.dg}
        self.cur_blocks = 0
        self.pieces = collections.deque()  # RAM: (blocks, nbytes, {channel: array})
        self.kept_bytes = 0
        self.stored_blocks = 0             # RAM: blocks appended so far
        self.dropped_blocks = 0            # RAM ring: blocks overwritten by newer data
        self.chunk = 0
        self.chunk_t0 = 0.0
        self.files = []
        self.index = None
        if storage == 'disk':
            os.makedirs(stem, exist_ok=True)
            self.index = {'version': 1, 'cmd': 'log', 'status': 'running', 'meta': meta_base, 'chunks': []}
            self._write_index()

    # -------------------------------------------------------------- producer side (USB loop thread)
    def put(self, bufs, n, info):
        with self.lock:
            self.queued += n
        self.q.put((bufs, n, info))

    def finish(self, info=None):
        """Stop the thread, print the last (partial) statistics line and close the files. After a processing error
        the data kept so far is still written and the index records the error."""
        self.q.put(None)
        self.join()
        info = dict(info or {})
        if self.error is not None:
            why = info.get('stop_reason')
            if type(self.error).__name__ == 'HookError':
                why = 'hook_error' if why in (None, 'duration', 'stop_request', 'hook') else why     # an earlier error stop stays named
            info.update(status='error', stop_reason=why or 'writer_error')
        try:
            if self.error is None:
                self._emit_line(final=True)
            self._close(info)
        except Exception as e:                       # noqa: BLE001 - reported, the data so far is kept
            self.error = self.error or e
        return self.error

    # -------------------------------------------------------------- consumer side
    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            bufs, n, info = item
            try:
                if self.error is None:
                    self._process(bufs, info)
            except BaseException as e:               # noqa: BLE001 - stops the run (stop_flag sees it)
                self.error = e
            finally:
                with self.lock:
                    self.queued -= n

    def _process(self, bufs, info):
        L = self.layout
        bb = L.block_bytes
        if self.hook is not None and self.hook.stop:
            return                                   # the hook ended the recording: nothing after that piece is kept
        raw = self.carry + b''.join(bufs)
        nb = len(raw) // bb
        self.carry = raw[nb * bb:]
        if nb == 0:
            return
        B = np.frombuffer(raw, np.uint8, count=nb * bb).reshape(nb, bb)
        first = self.blocks == 0
        t0_piece = self.blocks * packet.BLOCK_SAMPLES / self.rate
        an, dg = packet._decode_blocks(B, L, packed=True)
        out = {}
        for i, c in zip(L.analog, self.an):
            out[c] = an[i]
        for i, c in zip(L.digital, self.dg):
            p = dg[i]                                       # packed: np.packbits(bits, bitorder='little')
            if first and L.digital_stale:
                p = p.copy()                                # may be a view of the read-only USB bytes
                bits = np.unpackbits(p[:8], bitorder='little')
                bits[:L.digital_stale] = bits[L.digital_stale]   # stale level from the previous run
                p[:8] = np.packbits(bits, bitorder='little')
            out[c] = p
        for x in out.values():
            x.flags.writeable = False                # the hook sees the stored arrays: an in-place edit raises
        per = {c: x.size // nb for c, x in out.items()}              # array elements per block
        if self.storage == 'ram':
            nbytes = sum(int(x.size) for x in out.values())
            self.pieces.append((nb, nbytes, out))
            self.kept_bytes += nbytes
            self.stored_blocks += nb
            while self.ring and self.kept_bytes > self.ram_budget:
                b, nbytes0, arrs = self.pieces[0]           # the RAM ring drops its oldest samples
                per_block = nbytes0 // b
                drop = min(b, -(-(self.kept_bytes - self.ram_budget) // per_block))
                if drop >= b:
                    self.pieces.popleft()
                else:
                    self.pieces[0] = (b - drop, nbytes0 - drop * per_block,
                                      {c: x[drop * (x.size // b):] for c, x in arrs.items()})
                self.kept_bytes -= drop * per_block
                self.dropped_blocks += drop
        elif self.storage == 'disk':          # cut at exact chunk boundaries: chunk k starts at k x chunk_s
            b = 0
            while b < nb:
                take = min(nb - b, self.chunk_blocks - self.cur_blocks)
                for c, x in out.items():
                    self.cur[c].append(x[b * per[c]:(b + take) * per[c]])
                self.cur_blocks += take
                b += take
                if self.cur_blocks >= self.chunk_blocks:
                    self._save_chunk()
                    self.lacc['_chunk_saved'] = self.chunk - 1
        self.line_info = info
        b = 0
        while b < nb:                         # statistics, cut at exact line boundaries (every_s)
            take = min(nb - b, self.every_blocks - self.line_blocks)
            self._stats({c: x[b * per[c]:(b + take) * per[c]] for c, x in out.items()}, take)
            b += take
            if self.line_blocks >= self.every_blocks:
                self._emit_line()
        if self.hook is not None and not self.hook.stop:   # after storing: data up to a hook error is kept
            from . import hook as hookmod
            n_s = nb * packet.BLOCK_SAMPLES
            r = self.hook.call(hookmod.Block(out, t0_piece, n_s, self.rate, self.scale, self.an, self.dg))
            t_end = t0_piece + n_s / self.rate
            lag = None if self.live_s is None else max(0.0, self.live_s() - t_end)
            if lag is not None:
                self.hook.note_delay(lag)
            if r is not None and self.emit:
                self.emit({'event': r, 'k': self.hook.events - 1, 't_signal_s': round(t_end, 6),
                           'lag_s': None if lag is None else round(lag, 4),
                           't_wall_s': round(time.time() - (self.t0_wall or time.time()), 3)})
        if self.heartbeat:
            self.heartbeat()

    def _stats(self, seg, nb):
        n_s = nb * packet.BLOCK_SAMPLES
        for c in self.an:
            x = seg[c]
            mn, mx, sm = int(x.min()), int(x.max()), int(x.sum(dtype=np.uint64))
            a = self.acc[c]
            a['min'], a['max'], a['sum'], a['n'] = min(a['min'], mn), max(a['max'], mx), a['sum'] + sm, a['n'] + x.size
            la = self.lacc.setdefault(c, [255, 0, 0, 0])
            la[0], la[1], la[2], la[3] = min(la[0], mn), max(la[1], mx), la[2] + sm, la[3] + x.size
        for c in self.dg:
            d = self.dacc[c]
            ones, edges, d['last'] = packet.bit_stats(seg[c], d['last'])
            d['ones'] += ones
            d['edges'] += edges
            d['n'] += n_s
            la = self.lacc.setdefault(c, [0, 0, 0])
            la[0], la[1], la[2] = la[0] + ones, la[1] + edges, la[2] + n_s
        self.blocks += nb
        self.line_blocks += nb

    def _emit_line(self, extra=None, final=False):
        if not self.line_blocks:
            return
        info = self.line_info
        line = {'i': self.line, 't_signal_s': round(self.blocks * packet.BLOCK_SAMPLES / self.rate, 6),
                't_wall_s': round(info.get('t_wall', 0.0), 3), 'backlog_mb': round(info.get('backlog', 0) / 1e6, 2)}
        if 'guard_in' in info:                # the backlog grows: when the guard stops the run, what it will hold
            line.update(backlog_growth_mb_s=round(info['growth'] / 1e6, 1), ring_full_in_s=round(info['guard_in'], 1),
                        gapfree_projected_s=round(info['gapfree_projected'], 2))
        for c in self.an:
            a = self.lacc.get(c)
            if a:
                line[c] = {'vmin_v': self._v(c, a[0]), 'vmax_v': self._v(c, a[1]), 'vmean_v': self._v(c, a[2] / a[3])}
        for c in self.dg:
            a = self.lacc.get(c)
            if a:
                line[c] = {'high_frac': round(a[0] / a[2], 4), 'edges': a[1]}
        if '_chunk_saved' in self.lacc:
            line['chunk_saved'] = self.lacc['_chunk_saved']
        if self.ring:
            line['ram_kept_s'] = round((min(self.blocks, self.stored_blocks) - self.dropped_blocks)
                                       * packet.BLOCK_SAMPLES / self.rate, 3)
        if self.hook is not None:         # time spent in the hook since the last line
            line['hook_ms'] = round(self.hook.line_seconds * 1e3, 2)
            self.hook.line_seconds = 0.0
        line.update(extra or {})
        if final:
            line['final'] = True
        self.lacc, self.line_blocks = {}, 0
        self.line += 1
        if self.emit:
            self.emit(line)

    def _v(self, c, code):
        s = self.scale[c]
        return round((code - s['zero_code']) * s['volts_per_code'] * s['probe'], 4)

    def _save(self, path, t0, parts, nblocks, info=None):
        n = nblocks * packet.BLOCK_SAMPLES
        meta = json.loads(json.dumps(self.meta_base))
        meta['acq'] = dict(meta['acq'], samples=n, duration_s=n / self.rate, t_start_s=t0, **(info or {}))
        meta['t0_analog_s'] = meta['t0_digital_s'] = t0
        meta['digital_lengths'] = {c: n for c in self.dg}
        members = [('analog_' + c, parts[c]) for c in self.an] + [('digital_' + c, parts[c]) for c in self.dg]
        write_npz(path, meta, members)
        return path, n

    def _save_chunk(self):
        name = 'chunk_%05d.npz' % self.chunk
        _, n = self._save(os.path.join(self.stem, name), self.chunk_t0, self.cur, self.cur_blocks,
                          {'chunk': self.chunk})
        self.cur = {c: [] for c in self.cur}
        self.cur_blocks = 0
        self.index['chunks'].append({'file': name, 't0_s': self.chunk_t0, 'samples': n})
        self.files.append(os.path.join(self.stem, name))
        self.chunk += 1
        self.chunk_t0 += n / self.rate
        self._write_index()

    def _write_index(self):
        tmp = os.path.join(self.stem, 'index.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(self.index, f, indent=1)
        os.replace(tmp, os.path.join(self.stem, 'index.json'))

    def kept_t0(self):
        return self.dropped_blocks * packet.BLOCK_SAMPLES / self.rate

    def _close(self, info):
        if self.storage == 'ram' and self.pieces and self.stem:
            parts = {c: [p[2][c] for p in self.pieces] for c in self.an + self.dg}
            nblocks = sum(p[0] for p in self.pieces)
            extra = {'ram_ring': True, 'ram_dropped_s': round(self.kept_t0(), 6)} if self.ring else {}
            path, _ = self._save(self.stem + '.npz', self.kept_t0(), parts, nblocks, extra)
            self.files.append(path)
        elif self.storage == 'disk':
            if self.cur_blocks:
                self._save_chunk()
            self.index.update(status=info.get('status', 'done'), stop_reason=info.get('stop_reason'),
                              samples=self.blocks * packet.BLOCK_SAMPLES)
            self._write_index()

    def summary(self):
        out = {'analog': {}, 'digital': {}}
        for c, a in self.acc.items():
            if a['n']:
                out['analog'][c] = {'vmin_v': self._v(c, a['min']), 'vmax_v': self._v(c, a['max']),
                                    'vmean_v': self._v(c, a['sum'] / a['n'])}
        for c, d in self.dacc.items():
            if d['n']:
                out['digital'][c] = {'high_frac': round(d['ones'] / d['n'], 4), 'edges': d['edges'],
                                     'state': 'toggling' if d['edges'] else ('high' if d['ones'] else 'low')}
        return out


class BacklogMeter:
    """Data the scope has acquired but the host has not read yet (the loop ring's fill), seen from the host only:
    raw = D x (t - t_head) - bytes received, backlog = raw - baseline. The baseline absorbs the start offset and the
    scope / PC clock difference: it follows raw down at once (a backlog is never negative) and, while the estimate
    is below DRIFT_BAND (the host caught up), up by at most CLOCK_TOL x D per second. Above the band it stays put, so
    a real backlog is never absorbed beyond the band, and the estimate stays exact over hours whichever clock is
    faster (a fixed tolerance subtracted from D would hide ~110 MB of backlog after an hour at 150 MB/s). Growth
    slower than CLOCK_TOL x D cannot be told from clock drift. growth: backlog slope over the last ~2 s."""

    def __init__(self, data_bps, t_head):
        self.D, self.t0 = data_bps, t_head
        self.base, self.t_base = 0.0, t_head          # at the head nothing is behind yet
        self.backlog = self.max = self.growth = 0.0
        self.hist = []

    def update(self, now, received):
        raw = self.D * (now - self.t0) - received
        rise = CLOCK_TOL * self.D * max(0.0, now - self.t_base) if raw - self.base < DRIFT_BAND else 0.0
        self.base = min(raw, self.base + rise)
        self.t_base = now
        self.backlog = max(0.0, raw - self.base)
        self.max = max(self.max, self.backlog)
        h = self.hist
        if not h or now - h[-1][0] >= 0.25:
            h.append((now, self.backlog))
            while len(h) > 2 and now - h[0][0] > 2.0:
                h.pop(0)
            if len(h) >= 2 and h[-1][0] > h[0][0]:
                self.growth = max(0.0, (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0]))
        return self.backlog


STOP_ERRORS = {
    'backlog': ('HOST_TOO_SLOW', 'the host fell behind the scope by %.0f MB; the run was stopped before the 512 MiB ring '
                                 'could overwrite unread data', 'use a lower --rate or fewer channels'),
    'host_slow': ('HOST_TOO_SLOW', 'decoding / writing fell behind (%.0f MB queued); the run was stopped',
                  'use RAM storage, a faster disk, a lower --rate or fewer channels'),
    'stalled': ('TIMEOUT_DATA', 'no data from the scope for %.1f s', 'run "openm454 release"; if it persists replug the scope'),
}


def run(scope, st, pl, out_stem=None, emit=None, stop_flag=None, heartbeat=None, on_final=None, hook=None):
    """Run a planned log on an open Scope. Returns the result document (ok False with an error on a data-safety
    stop; the data up to the stop is kept). Device errors raise DscopeError after the files were closed.
    out_stem None: nothing is written (RAM data is dropped at the end). on_final(nbytes) is called before the
    final RAM file is written (the CLI extends its watchdog for the write)."""
    from .scope import queue_depth
    geom = dict(pl['_geom'])
    rate = pl['rate_hz']
    layout = packet.layout_from_regs(geom['_regs'], rates={'analog': rate, 'digital': rate})
    small = bool(geom['small_pkt'])
    blk = layout.block_bytes
    geom['transfer_count'] = planned_transfers(geom)
    data_bps = pl['_data_bps']
    piece_bytes = max(blk, min(pl['_every_blocks'] * blk, PIECE_BYTES // blk * blk))
    if hook is not None:                             # the hook reacts within about PIECE_S of signal
        from . import hook as hookmod
        piece_bytes = max(blk, min(piece_bytes, int(hookmod.PIECE_S * data_bps) // blk * blk))
        try:
            hook.setup({'rate_hz': rate, 'analog': list(st.analog), 'digital': list(st.digital),
                        'scale': {c: dict(pl['_scale'][c]) for c in st.analog}, 'every_s': pl['every_s'],
                        'duration_s': pl['duration_s'], 'storage': pl['storage'], 'block_samples': packet.BLOCK_SAMPLES,
                        'piece_s': piece_bytes // blk * packet.BLOCK_SAMPLES / rate})
        except hookmod.HookError as e:
            raise DscopeError('HOOK_ERROR', str(e), 'fix the hook file (nothing was recorded)') from None
    disk_limit = pl.get('_disk_bytes') if pl['storage'] == 'disk' else None
    ram_limit = pl.get('_ram_limit')
    target = None if pl['duration_s'] is None else int(math.ceil(pl['duration_s'] * rate / packet.BLOCK_SAMPLES)) * blk
    stall_s = max(2.0, 50 * pl['_pkt_s'])
    storage = pl['storage']
    if storage == 'disk' and not out_stem:
        raise DscopeError('INVALID_ARG', 'a disk log needs an output folder')
    stem = out_stem if storage in ('ram', 'disk') else None
    t0_wall = time.time()
    applied = scope_applied(st, pl)
    meta_base = {'version': 1, 'device': 'DSCope M454', 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                 'applied': applied, 'channels': {c: pl['_scale'][c] for c in st.analog},
                 'analog_rate_hz': rate if st.analog else None, 'digital_rate_hz': rate if st.digital else None,
                 'acq': {'mode': 'loop', 'analog_rate_hz': rate if st.analog else None,
                         'digital_rate_hz': rate if st.digital else None, 'triggered': False},
                 'trigger': {'source': None, 'mode': 'force'}, 'warnings': list(pl['warnings'])}
    w = _Writer(layout, rate, pl['_scale'], storage, pl['_chunk_blocks'], stem, meta_base, emit, heartbeat,
                pl['_ram_budget'], pl['ram_ring'], pl['_every_blocks'], hook=hook)
    w.t0_wall = t0_wall
    # how far behind live the hook is: the scope's newest sample = what arrived + its unread backlog (BacklogMeter)
    w.live_s = lambda: (s['n_data'] + s['backlog']) / data_bps
    w.start()
    s = {'head': False, 't_head': None, 't_last': None, 'n_usb': 0, 'n_data': 0, 'pend': [], 'pend_n': 0,
         'reason': None, 'backlog': 0, 'backlog_max': 0, 'queued_at_stop': 0, 'meter': None, 'tripped': False,
         'quiet': 0.0}
    quiet_s = max(0.25, 10 * pl['_pkt_s'])           # no data for this long: the scope stopped, not a slow host

    def projection():
        """Backlog growth over the last ~2 s and when the guard would stop the run (live lines)."""
        m = s['meter']
        if m is None or m.growth <= max(0.01 * data_bps, 0.25e6):     # not start-up noise at low rates
            return {}
        left = max(0.0, (BACKLOG_STOP - m.backlog) / m.growth)
        return {'growth': m.growth, 'guard_in': left,
                'gapfree_projected': (s['n_data'] + (data_bps - m.growth) * left) / data_bps}

    def flush(limit=None):
        bufs, n = s['pend'], s['pend_n']
        if limit is not None and n > limit:          # trim the end of the log to the requested duration
            keep, out = limit, []
            for b in bufs:
                if keep <= 0:
                    break
                out.append(b[:keep])
                keep -= len(out[-1])
            bufs, n = out, limit
        s['pend'], s['pend_n'] = [], 0
        if n:
            w.put(bufs, n, dict(projection(), t_wall=time.time() - t0_wall, backlog=s['backlog']))

    def on_chunk(buf):
        now = time.perf_counter()
        if not s['head']:
            s['head'], s['t_head'], s['t_last'] = True, now, now
            s['meter'] = BacklogMeter(data_bps, now)
            return
        s['t_last'] = now
        if s['tripped']:
            return                                   # read after the guard tripped: may be overwritten data
        if s['meter'].update(now, s['n_data']) > BACKLOG_STOP:
            s['tripped'] = True                      # per transfer: the engine calls stop() once per pass
            return
        s['n_usb'] += len(buf)
        d = buf[:blk] if small else buf             # small packets: one block, then filler, per 4 KiB read
        s['n_data'] += len(d)
        s['pend'].append(d)
        s['pend_n'] += len(d)
        if s['pend_n'] >= piece_bytes and (target is None or s['n_data'] < target):
            flush()                                  # the block that reaches the target is trimmed at the end

    def stop():
        if w.error is not None:
            s['reason'] = 'hook_error' if type(w.error).__name__ == 'HookError' else 'writer_error'
            return True
        if hook is not None and hook.stop:
            s['reason'] = 'hook'                     # the hook asked to stop: a normal end
            return True
        if stop_flag is not None and stop_flag():
            s['reason'] = 'stop_request'
            return True
        if not s['head']:
            return False
        now = time.perf_counter()
        s['backlog'] = s['meter'].update(now, s['n_data'])
        s['backlog_max'] = s['meter'].max
        s['quiet'] = now - s['t_last']
        if s['tripped'] or s['backlog'] > BACKLOG_STOP:
            # a forced log planned to end here: ring_full; nothing arriving at all: the scope stopped sending
            s['reason'] = 'stalled' if s['quiet'] > quiet_s else ('ring_full' if pl.get('_force') else 'backlog')
            return True
        if target is not None and s['n_data'] >= target:
            s['reason'] = 'duration'
            return True
        if disk_limit is not None and s['n_data'] >= disk_limit:
            s['reason'] = 'disk_allowance'
            return True
        if ram_limit is not None and s['n_data'] >= ram_limit:
            s['reason'] = 'ram_budget'
            return True
        if s['quiet'] > stall_s:
            s['reason'] = 'stalled'
            return True
        if w.queued > QUEUE_LIMIT_BYTES:
            s['reason'] = 'host_slow'
            s['queued_at_stop'] = w.queued
            return True
        return False

    raw = None
    err = None
    try:
        raw = scope._run(pl['_prog'], geom, 3.0, None, on_chunk=on_chunk, stop_flag=stop, poll=False, keep_data=False)
    except BaseException as e:                       # device error: close the files first, then re-raise
        err = e
    already = sum(len(b) for b in s['pend'])
    sent = s['n_data'] - already
    flush(None if target is None else max(0, target - sent))
    if isinstance(err, KeyboardInterrupt):
        status, reason = 'interrupted', 'interrupted'
    else:
        status = 'error' if err is not None or s['reason'] in STOP_ERRORS or s['reason'] in ('writer_error',
                                                                                            'hook_error') else 'done'
        reason = s['reason'] or ('device_error' if err else None)
    if on_final is not None and storage == 'ram' and stem:
        on_final(w.kept_bytes + w.queued)
    werr = w.finish({'status': status, 'stop_reason': reason})
    if werr is not None and type(werr).__name__ == 'HookError' and s['reason'] in (None, 'duration', 'stop_request', 'hook'):
        s['reason'] = 'hook_error'                   # the data kept ends where the hook failed
    if err is not None:
        try:
            err.log_files = _files(storage, w, stem, rate)
            if hook is not None:                     # the hook still hears the end, and its report is kept
                err.hook_report = _hook_close(hook, w.blocks * packet.BLOCK_SAMPLES, rate, reason,
                                              finish=werr is None)[0]
        except Exception:                            # noqa: BLE001 - only an annotation
            pass
        raise err
    if (raw is None or raw.head is None) and s['reason'] == 'stop_request':
        return {'ok': True, 'cmd': 'log', 'plan': public(pl), 'files': _files(storage, w, stem, rate),
                'acq': {'mode': 'loop', 'samples': 0, 'stop_reason': 'stop_request'},
                'note': 'stopped on request before any data arrived'}
    if raw is None or raw.head is None:
        raise DscopeError('PROTOCOL_ERROR', 'no data from the scope (no head within 3 s)',
                          'run "openm454 release"; if it persists replug the scope')
    n_samples = w.blocks * packet.BLOCK_SAMPLES
    span = (s['t_last'] - s['t_head']) if s['t_head'] and s['t_last'] else 0.0
    acq = {'mode': 'loop', 'rate_hz': rate, 'samples': n_samples, 'duration_s': round(n_samples / rate, 6),
           'data_mb': round(s['n_data'] / 1e6, 3), 'usb_mb': round(s['n_usb'] / 1e6, 3),
           'host_mb_s': round(s['n_usb'] / span / 1e6, 3) if span > 0 else None,
           'backlog_max_mb': round(s['backlog_max'] / 1e6, 2), 'stop_reason': s['reason'],
           'unread_at_stop_mb': round(s['backlog'] / 1e6, 1),
           'head_word': '%08X' % int.from_bytes(raw.head[8:12], 'little'), 'stop': raw.stop,
           **queue_depth(raw), 'elapsed_s': round(time.time() - t0_wall, 3)}
    res = {'ok': True, 'cmd': 'log', 'plan': public(pl), 'acq': acq}
    res.update(w.summary())
    if hook is not None:
        res['hook'], ferr = _hook_close(hook, n_samples, rate, s['reason'], finish=werr is None)
        werr = werr or ferr
    res['files'] = _files(storage, w, stem, rate)
    res['warnings'] = list(pl['warnings'])
    if hook is not None and ((res['hook']['load'] or 0) > 1.0 or (res['hook']['lag_max_s'] or 0) > 1.0):
        res['warnings'].append('the hook fell behind: it needed %.2f s per second of signal and was up to %.1f s '
                               'behind live; keep it lighter (numpy on block.codes() / packed(), less work per piece) '
                               'or lower --rate' % (res['hook']['load'] or 0, res['hook']['lag_max_s'] or 0.0))
    if s['reason'] == 'ring_full':
        res['warnings'].append('--force: stopped after %.2f s of gap-free signal when the scope was %.0f MB ahead; the '
                               'unread part (acquired after that) is lost' % (n_samples / rate, s['backlog'] / 1e6))
    if s['reason'] == 'ram_budget':
        res['warnings'].append('stopped at the RAM budget (%.2f GB)' % (ram_limit / 1e9))
    remembered = None
    if s['reason'] in ('duration', 'ring_full', 'backlog', 'stop_request') and data_bps > SUSTAINED_BPS and span >= 1.0:
        remembered = _remember_readout(pl, s['meter'], s['n_data'] / span, data_bps)
    if s['reason'] == 'disk_allowance':
        res['warnings'].append('stopped at the disk allowance (%.1f GB); raise it with openm454 bench --disk'
                               % (disk_limit / 1e9))
    if werr is not None and type(werr).__name__ == 'HookError':
        res['ok'] = False
        res['error'] = {'code': 'HOOK_ERROR', 'message': '%s (data up to the stop is kept)' % werr,
                        'hint': 'fix the hook file; its traceback is in error.trace', 'trace': werr.trace}
    elif werr is not None:
        res['ok'] = False
        res['error'] = {'code': 'WRITE_FAILED', 'message': 'writing the log failed: %r' % (werr,),
                        'hint': 'check the free space and permissions of --out'}
    elif s['reason'] in STOP_ERRORS:
        code, msg, hint = STOP_ERRORS[s['reason']]
        val = {'backlog': s['backlog'] / 1e6, 'host_slow': s['queued_at_stop'] / 1e6, 'stalled': s['quiet']}[s['reason']]
        if s['reason'] == 'backlog' and remembered and not remembered[1]:
            hint = ('this channel set reads out at %.0f MB/s here (now remembered): log --plan shows how long a log '
                    'stays gap-free and what stream gives; or %s' % (remembered[0], hint))
        res['ok'] = False
        res['error'] = {'code': code, 'message': msg % val + ' (data up to the stop is kept)', 'hint': hint}
    return res


def _hook_close(hook, n_samples, rate, reason, finish=True):
    """The hook's report, with finish(summary) called first (not after a process() error). -> (report, HookError or
    None); a finish() error is also noted in the report."""
    from . import hook as hookmod
    rep, ferr = hook.report(n_samples / rate), None
    if finish:
        try:
            r = hook.finish({'samples': n_samples, 'duration_s': n_samples / rate, 'stop_reason': reason,
                             'calls': hook.calls, 'events': hook.events})
            if r is not None:
                rep['result'] = r
        except hookmod.HookError as e:
            ferr = e
            rep['finish_error'] = str(e)
    return rep, ferr


def _remember_readout(pl, meter, run_bps, data_bps):
    """A run above the sustained rate measures the readout of its channel set, cached for the next plan: exactly when
    the backlog was still growing at the stop and the run's average read rate agrees with D - growth (a steady
    readout limit, not a stall at the end); as the lower bound R >= D when the host was caught up at the end; else
    nothing (it ended while catching up). -> (MB/s, lower_bound) stored, or None."""
    from . import host
    if meter is None:
        return None
    if meter.growth > 0.01 * data_bps and meter.backlog > DRIFT_BAND \
            and abs((data_bps - meter.growth) - run_bps) < 0.03 * data_bps:
        mb, lower = run_bps / 1e6, False
    elif meter.backlog < DRIFT_BAND and meter.growth <= 0.005 * data_bps:
        mb, lower = data_bps / 1e6, True
    else:
        return None
    try:
        host.readout_put(pl['_readout_key'], mb, lower, data_bps / 1e6)
    except (OSError, ValueError, KeyError):
        return None
    return mb, lower


def _files(storage, w, stem, rate):
    files = {}
    if storage == 'ram' and w.files:
        files['npz'] = w.files[0]
        files['t0_s'] = round(w.kept_t0(), 6)
        files['kept_s'] = round((w.stored_blocks - w.dropped_blocks) * packet.BLOCK_SAMPLES / rate, 6)
    elif storage == 'disk' and stem:
        files['dir'] = stem
        files['index'] = os.path.join(stem, 'index.json')
        files['chunks'] = len(w.files)
    return files


def scope_applied(st, pl):
    sc = pl['_scale']
    return {'channels': list(st.analog) + list(st.digital),
            'vdiv_v': {c: round(sc[c]['vdiv_v'] * sc[c]['probe'], 6) for c in st.analog},
            'offset_v': {c: round((128 - sc[c]['zero_code']) * sc[c]['volts_per_code'] * sc[c]['probe'], 5)
                         for c in st.analog},
            'probe': {c: sc[c]['probe'] for c in st.analog},
            'coupling': {c: st.coupling.get(c, 'dc') for c in st.analog}, 'bw20': st.bw20, 'invert': st.invert,
            'threshold_v': pl['_hal']['threshold_uv'] / 1e6, 'rate_hz': pl['rate_hz'], 'mode': 'loop'}
