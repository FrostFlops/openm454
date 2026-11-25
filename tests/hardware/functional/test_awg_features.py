"""Every generator feature, seen through the declared link (AWG -> A0): marker hw, serial, never in CI.

bench.json declares a square; the other waveforms check themselves against the frequency and level they were
programmed with, because on this bench the generator is the only source and the capture is the only observer.
"""
import statistics

import pytest

pytestmark = pytest.mark.hw

RATE = 10e6
WINDOW = 2e-3


def _capture(scope, link, duration=WINDOW, rate=RATE):
    cap = scope.capture(link.settings(duration, rate))
    assert cap.meta['acq']['complete'], 'a measurement needs a complete capture'
    return cap, cap.measure(link.channel)


def _freq(m, want, tol):
    got = m.get('freq_hz')
    assert got is not None and abs(got - want) <= tol['freq_rel'] * want, \
        'measured %s Hz, programmed %g Hz' % (got, want)


def _vpp(m, want, tol):
    assert abs(m['vpp_v'] - want) <= tol['level_v'], \
        'measured %g Vpp, programmed %g V' % (m['vpp_v'], want)


def test_a_sine_arrives_at_the_programmed_frequency_and_level(bench, scope):
    link = bench.link('awg_to_a0')
    tol = link['tolerance']
    scope.awg_set('sine', 1e3, 3.0, 0.0)
    _, m = _capture(scope, link)
    _freq(m, 1e3, tol)
    _vpp(m, 3.0, tol)


def test_a_triangle_arrives(bench, scope):
    link = bench.link('awg_to_a0')
    tol = link['tolerance']
    scope.awg_set('triangle', 1e3, 3.0, 0.0)
    _, m = _capture(scope, link)
    _freq(m, 1e3, tol)
    _vpp(m, 3.0, tol)


@pytest.mark.parametrize('duty', [10.0, 50.0, 90.0])
def test_square_duty_is_what_was_programmed(bench, scope, duty):
    link = bench.link('awg_to_a0')
    tol = link['tolerance']
    scope.awg_set('square', 1e3, 3.0, 0.0, duty=duty)
    _, m = _capture(scope, link)
    _freq(m, 1e3, tol)
    assert abs(m['duty_pct'] - duty) <= tol['duty_abs_pct'], \
        'measured %g %% duty, programmed %g %%' % (m['duty_pct'], duty)


def test_a_burst_delivers_the_programmed_number_of_cycles(bench, scope):
    """Three 10 kHz cycles every millisecond for five milliseconds: thirty square edges, plus the boundaries."""
    link = bench.link('awg_to_a0')
    scope.awg_burst('square', 10e3, 3.0, 0.0, cycles=3, period=1e-3)
    cap, _ = _capture(scope, link, duration=5e-3)
    rises = cap.measure(link.channel, edges_n=400).get('edges_rise_s') or []
    want = 3 * 5                                   # three cycles every millisecond, five milliseconds
    assert abs(len(rises) - want) <= 4, 'measured %d rising edges in 5 ms, programmed about %d' % (len(rises), want)


def test_a_sweep_shortens_the_period(bench, scope):
    link = bench.link('awg_to_a0')
    scope.awg_sweep(1e3, 10e3, 20e-3, sweep_type='linear', wave='square')
    cap, _ = _capture(scope, link, duration=20e-3)
    edges = cap.measure(link.channel, edges_n=400).get('edges_rise_s') or []
    assert len(edges) > 40
    gaps = [b - a for a, b in zip(edges, edges[1:])]
    # The 20 ms window is not aligned with the 20 ms sweep, so it always holds one restart (back to 1 kHz) at a random
    # place; comparing the two halves of the window failed whenever the restart fell in the second half (5 of 10 runs).
    # Split at the restarts (a period that jumps up by 1.5x) and judge each stretch of the sweep on its own.
    segments, cur = [], [gaps[0]]
    for prev, g in zip(gaps, gaps[1:]):
        if g > 1.5 * prev:
            segments.append(cur)
            cur = []
        cur.append(g)
    segments.append(cur)
    long_ = [s for s in segments if len(s) >= 10]
    assert long_, 'no stretch of the sweep with 10 periods between restarts: %s' % [len(s) for s in segments]
    for s in long_:
        half = len(s) // 2
        assert statistics.median(s[:half]) > statistics.median(s[half:]), \
            'an up sweep has to shorten the period, not lengthen it'
    # one full sweep period is in the window: both ends of 1 kHz .. 10 kHz show up
    assert min(gaps) < 0.2e-3 and max(gaps) > 0.5e-3, 'periods %.3g .. %.3g s' % (min(gaps), max(gaps))


def test_am_modulation_shows_an_envelope(bench, scope):
    link = bench.link('awg_to_a0')
    scope.awg_modulate('am', 'sine', 100e3, 3.0, 0.0, mod_freq=1e3, depth=50.0)
    cap, _ = _capture(scope, link, duration=2e-3, rate=100e6)
    v = cap.volts(link.channel)
    n = 1000                                   # 10 us: one carrier period, far below the 1 ms envelope
    k = len(v) // n * n
    chunks = v[:k].reshape(-1, n)
    ptp = chunks.max(axis=1) - chunks.min(axis=1)
    assert ptp.max() / ptp.min() > 1.5, \
        'a 50 %% AM envelope has to change the amplitude (max/min was %.2f)' % (ptp.max() / ptp.min())


def test_dc_is_a_steady_level(bench, scope):
    link = bench.link('awg_to_a0')
    scope.awg_dc(1.5)
    _, m = _capture(scope, link)
    assert abs(m['vmean_v'] - 1.5) <= link['tolerance']['level_v']
    assert m['vpp_v'] < 0.15


def test_off_parks_the_output(bench, scope):
    link = bench.link('awg_to_a0')
    scope.awg_off()
    _, m = _capture(scope, link)
    assert m['vpp_v'] < 0.15 and abs(m['vmean_v']) <= 0.2