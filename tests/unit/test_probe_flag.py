"""Scope.record(probe=...): an explicit flag decides, the automatic rule only fills in for None.

The review found the two flags inverted in effect — probe=False on an uncached 200 MSa/s recording still probed,
probe=True on a 1 MSa/s recording skipped it — because the plan decided on "uncached and above the sustained rate"
alone and Scope.record only used the flag to defer refusals and to ignore the cache.
"""
import types

import pytest

from openm454 import host, logger
from openm454 import scope as scopemod
from tests.support.logging import settings


@pytest.fixture(autouse=True)
def no_host_file(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'HOST_FILE', str(tmp_path / 'host.json'))


@pytest.fixture
def probed(monkeypatch):
    """How many readout probes Scope.record(rate, probe=...) asks for, without a device."""
    calls = []
    monkeypatch.setattr(logger, 'measure_readout', lambda scope, pl: calls.append(pl['_readout_key']))
    monkeypatch.setattr(logger, 'run', lambda scope, st, pl, *a, **kw: {'ok': True})
    stub = types.SimpleNamespace(_check_link=lambda: None, _cal_check=lambda st: None,
                                dev=types.SimpleNamespace(zero_info=None))

    def record(rate, probe, channels=('A0',)):
        calls.clear()
        scopemod.Scope.record(stub, settings(list(channels), []), rate, 5, probe=probe)
        return len(calls)
    return record


def test_probe_false_never_measures(probed):
    assert probed(200e6, False) == 0, 'the flag says never, even for an unmeasured set above the sustained rate'
    assert probed(200e6, None) == 1, 'None keeps the automatic rule: unmeasured and above the sustained rate'
    assert probed(1e6, None) == 0, 'below the sustained rate there is nothing to learn'


def test_probe_true_always_measures(probed):
    assert probed(1e6, True) == 1, 'below the sustained rate, but the flag says measure'
    assert probed(1e6, False) == 0
