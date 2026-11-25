"""Loop-mode recording for a whole soak tier, to disk: every record closes, nothing grows (hwsoak)."""
import time

import pytest

from tests.support import soak as soakmod

pytestmark = [pytest.mark.hw, pytest.mark.hwsoak]


def test_log_soak(bench, scope, soak, duration, soak_cfg, tmp_path, disk_guard):
    link = bench.link('awg_to_a0')
    link.program(scope)
    rate = float(soak_cfg.get('log_rate_hz', 1e6))
    record_s = float(soak_cfg.get('log_record_s', 5))
    st = link.settings(1e-3, rate)
    # the whole run lands in tmp_path: check that filesystem for the worst case (one byte per analog sample),
    # not the checkout and not a fixed floor - a one-hour run at 1 MSa/s is 3.6 GB
    records_planned = max(1, int(-(-duration // record_s)))
    disk = disk_guard(tmp_path, records_planned * record_s * rate, 'the log soak')
    before = soak.snapshot()
    runs, samples, rss = 0, 0, []
    t_end = time.time() + duration
    while time.time() < t_end:
        res = scope.record(st, rate, duration=record_s, storage='disk', out_stem=str(tmp_path / ('log%d' % runs)))
        assert res['ok'], res.get('error')
        assert res['acq']['mode'] == 'loop' and res['acq']['samples'] > 0
        want_mb = record_s * rate / 1e6                       # one byte per analog sample
        assert abs(res['acq']['data_mb'] - want_mb) <= 0.05 * want_mb, 'a record has to hold the samples it claims'
        samples += int(res['acq']['samples'])
        runs += 1
        rss.append(soak.snapshot()['rss_mib'])
        soak.row(run=runs, samples=res['acq']['samples'], rss_mib=rss[-1])
    growth = soak.growth(before, soak.snapshot())
    written = sum(f.stat().st_size for f in tmp_path.rglob('*') if f.is_file()) if tmp_path.exists() else 0
    files = sum(1 for f in tmp_path.rglob('*') if f.is_file()) if tmp_path.exists() else 0
    checks = {
        'records': {'value': runs, 'ok': runs > 0},
        'files': {'value': files, 'ok': files >= runs},          # disk storage: <out_stem>/ with chunks + index
        'samples': {'value': samples, 'ok': samples == runs * int(record_s * rate)},
        'bytes_written': {'value': written, 'ok': written > 0},
        'rss_after_warmup_mib': soakmod.net_growth(rss, float(soak_cfg.get('rss_after_warmup_mib', 32))),
        'rss_steady_mib': soakmod.steady_growth(rss, float(soak_cfg.get('rss_steady_mib', 8)),
                                                       int(soak_cfg.get('rss_steady_min_samples', 40))),
        'fds': {'value': growth['fds'], 'ok': growth['fds'] in (None, 0)},
        'threads': {'value': growth['threads'], 'ok': growth['threads'] in (None, 0)},
        'device_released': {'value': scope.dev.t.running,
                            'ok': scope.dev.t.running is False and not scope.dev.t.failed},
    }
    soak.finish(checks, notes={'record_s': record_s, 'rate_hz': rate, 'disk': disk})
    assert not any(v.get('ok') is False for v in checks.values() if isinstance(v, dict)), checks
