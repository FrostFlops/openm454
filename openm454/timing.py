"""Measured-fidelity timing profiles (experimental branch only, not for main).

The spacing constants in acquire/transport/scope reproduce DSWave's recorded behaviour, which is how the driver keeps
its fidelity without measuring anything: if a run is spaced like DSWave spaced it, the device cannot tell the
difference. This module is the alternative policy for the `exp/measured-timing` branch: keep the constants as the
baseline, and let a profile shorten them for an experiment whose fidelity is judged by the signal that comes back.

    OPENM454_TIMING=gap=0.005,submit=0.02,setup=0.005,start=0.005,read=0.003

Keys (seconds): gap GAP_BETWEEN_RUNS, submit SUBMIT_TO_START, setup SETUP_SETTLE, start START_SETTLE,
final FINAL_GAP, read READ_DELAY, wake ANALOG_WAKE_SETTLE, overdrive OVERDRIVE_SETTLE.
No profile means no change: main's behaviour is byte-identical without the environment variable.

Bench tooling that judges the result: bench/timing_sweep.py (captures per second vs fidelity) and
bench/read_delay_probe.py (open time vs register read-back consistency) in the easy521-iotest repo.
"""
import os

ENV = 'OPENM454_TIMING'
KEYS = {'gap': 'GAP_BETWEEN_RUNS', 'submit': 'SUBMIT_TO_START', 'setup': 'SETUP_SETTLE',
        'start': 'START_SETTLE', 'final': 'FINAL_GAP', 'read': 'READ_DELAY',
        'wake': 'ANALOG_WAKE_SETTLE', 'overdrive': 'OVERDRIVE_SETTLE'}


def profile(spec=None):
    """'gap=0.005,submit=0.02' -> {'GAP_BETWEEN_RUNS': 0.005, 'SUBMIT_TO_START': 0.02}; unknown keys raise."""
    spec = os.environ.get(ENV) if spec is None else spec
    out = {}
    for item in (spec or '').replace(' ', '').split(','):
        if not item:
            continue
        key, _, value = item.partition('=')
        if key not in KEYS:
            raise ValueError('%s: unknown timing key %r (have %s)' % (ENV, key, ', '.join(sorted(KEYS))))
        out[KEYS[key]] = float(value)
    return out


def apply(namespace, spec=None):
    """Apply the profile to a module's globals; returns the names it changed (empty without a profile)."""
    changed = {}
    for name, value in profile(spec).items():
        if name in namespace:
            changed[name] = namespace[name]
            namespace[name] = value
    return changed
