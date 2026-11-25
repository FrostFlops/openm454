"""Open/close cycles for a whole soak tier: every reopen works and does not get slower (hwsoak)."""
import time

import pytest

from tests.support import soak as soakmod

pytestmark = [pytest.mark.hw, pytest.mark.hwsoak]


def test_lifecycle_soak(bench, scope, soak, duration, soak_cfg):
    link = bench.link('awg_to_a0')
    link.program(scope)
    before = soak.snapshot()
    opens, captures, rss = [], 0, []
    t_end = time.time() + duration
    while time.time() < t_end:
        cap = scope.capture(link.settings(2e-3, 10e6))
        assert cap.meta['acq']['complete']
        captures += 1
        scope.close()
        t0 = time.time()
        scope.open()
        opens.append(time.time() - t0)
        rss.append(soak.snapshot()['rss_mib'])
        soak.row(open_ms=round(opens[-1] * 1e3, 2), captures=captures, rss_mib=rss[-1])
    growth = soak.growth(before, soak.snapshot())
    drift = soakmod.drift(opens, float(soak_cfg.get('open_time_drift', 0.2)))
    checks = {
        'cycles': {'value': captures, 'ok': captures == len(opens) and captures > 0},
        'open_ms_median': {'value': round(sorted(opens)[len(opens) // 2] * 1e3, 1), 'ok': min(opens) > 0},
        'open_drift': drift,
        'rss_after_warmup_mib': soakmod.net_growth(rss, float(soak_cfg.get('rss_after_warmup_mib', 32))),
        'rss_steady_mib': soakmod.steady_growth(rss, float(soak_cfg.get('rss_steady_mib', 8)),
                                                       int(soak_cfg.get('rss_steady_min_samples', 40))),
        'fds': {'value': growth['fds'], 'ok': growth['fds'] in (None, 0)},
        'threads': {'value': growth['threads'], 'ok': growth['threads'] in (None, 0)},
        'device_released': {'value': scope.dev.t.running,
                            'ok': scope.dev.t.running is False and not scope.dev.t.failed},
    }
    soak.finish(checks, notes={'cycles': captures})
    assert not any(v.get('ok') is False for v in checks.values() if isinstance(v, dict)), checks
