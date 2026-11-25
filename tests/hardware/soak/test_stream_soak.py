"""Streaming for a whole soak tier, record after record: rows keep coming, the signal clock within a record is
monotone, nothing grows (hwsoak)."""
import time

import pytest

from tests.support import soak as soakmod

pytestmark = [pytest.mark.hw, pytest.mark.hwsoak]


def test_stream_soak(bench, scope, soak, duration, soak_cfg):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(float(soak_cfg.get('stream_window_s', 0.2)), 10e6)
    every = float(soak_cfg.get('stream_every_s', 0.05))
    before = soak.snapshot()
    records, rss, clock = 0, [], []
    t_end = time.time() + duration

    def emit(line):
        clock.append((records, line.get('t_signal_s')))

    while time.time() < t_end:
        cap = scope.stream(st, every=every, emit=emit, build=False)      # one record; decode only if needed later
        assert cap.meta['acq']['complete'], 'every streamed record has to be complete'
        records += 1
        rss.append(soak.snapshot()['rss_mib'])
        soak.row(record=records, samples=cap.meta['acq']['samples'], rows=len(clock), rss_mib=rss[-1])
    growth = soak.growth(before, soak.snapshot())
    per_record = {}
    for index, t_sig in clock:
        if t_sig is not None:
            per_record.setdefault(index, []).append(t_sig)
    monotone = all(v == sorted(v) for v in per_record.values())
    checks = {
        'records': {'value': records, 'ok': records > 0},
        'rows': {'value': len(clock), 'ok': len(clock) > 0},
        'monotone_signal_clock': {'value': monotone, 'ok': monotone},
        'rss_after_warmup_mib': soakmod.net_growth(rss, float(soak_cfg.get('rss_after_warmup_mib', 64))),
        'rss_steady_mib': soakmod.steady_growth(rss, float(soak_cfg.get('rss_steady_mib', 8)),
                                                       int(soak_cfg.get('rss_steady_min_samples', 40))),
        'fds': {'value': growth['fds'], 'ok': growth['fds'] in (None, 0)},
        'threads': {'value': growth['threads'], 'ok': growth['threads'] in (None, 0)},
        'device_released': {'value': scope.dev.t.running,
                            'ok': scope.dev.t.running is False and not scope.dev.t.failed},
    }
    soak.finish(checks, notes={'record_s': st.duration, 'every_s': every, 'records': records})
    assert not any(v.get('ok') is False for v in checks.values() if isinstance(v, dict)), checks
