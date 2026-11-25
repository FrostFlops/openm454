"""The research scripts drive the generator, so they pass the same per-run wiring confirmation the hardware suite does.

Offline: the gate has to refuse before the scope is opened, so nothing has to be attached to test it.
"""
import pytest

from tests.hardware.research import signal_quality, usb_ceiling


class _PastTheGate(Exception):
    """Raised by the stub scope: reaching it proves the argument was accepted and the script went on to open."""


@pytest.mark.parametrize('script', [usb_ceiling, signal_quality], ids=['usb_ceiling', 'signal_quality'])
def test_an_unconfirmed_run_refuses_before_reaching_the_scope(script, monkeypatch):
    def no_scope(*a, **kw):
        pytest.fail('the scope was opened before the wiring was confirmed')
    monkeypatch.setattr(script.scopemod, 'Scope', no_scope)
    with pytest.raises(SystemExit) as exit_info:
        script.main([])
    message = str(exit_info.value.code)
    assert '--bench-confirmed' in message and 'tests/hardware/bench.json' in message
    assert 'awg_to_a0' in message, 'the refusal lists the links the declaration assumes'


@pytest.mark.parametrize('script', [usb_ceiling, signal_quality], ids=['usb_ceiling', 'signal_quality'])
def test_a_confirmed_run_gets_past_the_gate(script, monkeypatch):
    def stop_here(*a, **kw):
        raise _PastTheGate()
    monkeypatch.setattr(script.scopemod, 'Scope', stop_here)
    with pytest.raises(_PastTheGate):                # not SystemExit: the confirmation was accepted
        script.main(['--bench-confirmed'])
