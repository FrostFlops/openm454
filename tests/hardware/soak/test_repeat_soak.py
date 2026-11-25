"""Repeat mode for a whole soak tier: frames keep arriving, stamps stay monotone, nothing grows (hwsoak)."""
import statistics
import time

import pytest

from tests.support import soak as soakmod

pytestmark = [pytest.mark.hw, pytest.mark.hwsoak]


def test_repeat_soak(bench, scope, soak, duration, soak_cfg):
    link = bench.link('awg_to_a0')
    link.program(scope)
    frames = int(soak_cfg.get('repeat_frames', 512))
    window = float(soak_cfg.get('repeat_window_s', 2e-3))
    st = link.settings(window, 10e6)
    st.repeat = frames
    before = soak.snapshot()
    medians, total, worst_gap, rss = [], 0, 0.0, []
    t_end = time.time() + duration
    while time.time() < t_end:
        cap = scope.capture(st)
        stamps = [f.get('host_s') for f in cap.frames]
        assert len(stamps) == frames, 'a repeat run has to come back with every frame'
        assert all(s is not None for s in stamps), 'every frame has to carry its host arrival time'
        assert stamps == sorted(stamps), 'arrival times have to be monotone'
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        worst_gap = max(worst_gap, max(gaps))
        medians.append(statistics.median(gaps) * 1e3)
        total += len(stamps)
        rss.append(soak.snapshot()['rss_mib'])
        soak.row(frames=len(stamps), gap_median_ms=round(medians[-1], 4), gap_max_ms=round(max(gaps) * 1e3, 4),
                 rss_mib=rss[-1])
    growth = soak.growth(before, soak.snapshot())
    drift = soakmod.drift(medians, float(soak_cfg.get('drift_tolerance', 0.01)))
    back = scope.capture(link.settings(window, 10e6))
    checks = {
        'frames_total': {'value': total, 'ok': total == frames * len(medians)},
        'frames_per_capture': {'value': frames, 'ok': all(m > 0 for m in medians)},
        'max_gap_ms': {'value': round(worst_gap * 1e3, 4),
                       'ok': worst_gap * 1e3 <= float(soak_cfg.get('max_gap_ms', 50))},
        'drift': drift,
        'rss_after_warmup_mib': soakmod.net_growth(rss, float(soak_cfg.get('rss_after_warmup_mib', 32))),
        'rss_steady_mib': soakmod.steady_growth(rss, float(soak_cfg.get('rss_steady_mib', 8)),
                                                       int(soak_cfg.get('rss_steady_min_samples', 40))),
        'fds': {'value': growth['fds'], 'ok': growth['fds'] in (None, 0)},
        'threads': {'value': growth['threads'], 'ok': growth['threads'] in (None, 0)},
        'device_released': {'value': scope.dev.t.running,
                            'ok': scope.dev.t.running is False and not scope.dev.t.failed},
        'capture_after': {'value': bool(back.meta['acq']['complete']),
                          'ok': bool(back.meta['acq']['complete'])},
    }
    soak.finish(checks, notes={'captures': len(medians), 'window_s': window})
    assert not any(v.get('ok') is False for v in checks.values() if isinstance(v, dict)), checks
