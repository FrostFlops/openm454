"""OPENM454_TIMING: the measured-fidelity profile hook (experimental branch).

Without the variable nothing changes — that is the contract that keeps main safe while the branch lets an experiment
shorten the spacing. The values themselves are only worth shortening if the signal still matches: see
bench/timing_sweep.py and bench/read_delay_probe.py in the easy521-iotest repo.
"""
import os

import pytest

from openm454 import timing


def test_no_profile_changes_nothing():
    ns = {'GAP_BETWEEN_RUNS': 0.1, 'READ_DELAY': 0.015}
    assert timing.apply(ns, spec='') == {}
    assert ns == {'GAP_BETWEEN_RUNS': 0.1, 'READ_DELAY': 0.015}


def test_parse_and_apply():
    ns = {'GAP_BETWEEN_RUNS': 0.1, 'SUBMIT_TO_START': 0.05, 'READ_DELAY': 0.015, 'UNRELATED': 1}
    changed = timing.apply(ns, spec='gap=0.005,submit=0.02,read=0.003')
    assert changed == {'GAP_BETWEEN_RUNS': 0.1, 'SUBMIT_TO_START': 0.05, 'READ_DELAY': 0.015}
    assert ns['GAP_BETWEEN_RUNS'] == 0.005 and ns['SUBMIT_TO_START'] == 0.02 and ns['READ_DELAY'] == 0.003
    assert ns['UNRELATED'] == 1                    # names the module does not have are left alone


def test_unknown_key_is_refused():
    with pytest.raises(ValueError):
        timing.profile('gap=0.005,nonsense=1')


def test_the_environment_variable_is_read(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'wake=0.01,overdrive=0.02')
    assert timing.profile() == {'ANALOG_WAKE_SETTLE': 0.01, 'OVERDRIVE_SETTLE': 0.02}


def test_the_modules_apply_it_at_import(monkeypatch):
    """A fresh interpreter with the variable set: the module-level constant is the profile's value."""
    import subprocess
    import sys
    env = dict(os.environ, **{timing.ENV: 'gap=0.007'})
    out = subprocess.run([sys.executable, '-c', 'import openm454.acquire as a; print(a.GAP_BETWEEN_RUNS)'],
                         capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.strip() == '0.007', out.stderr
