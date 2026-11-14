"""Repeat mode exposure: settings -> hal_config -> capture, without a device.

The packet-level frame layout (tests/test_packet_golden.py::test_repeat_mode_frames) and the engine path
(tests/test_acquire_sim.py) are covered elsewhere; what these pin is the wiring that made the mode unreachable:
`hal['repeat']` used to be a hard 0 in hal_config, so nothing could turn it on.
"""
import pytest

from openm454 import scope
from openm454.errors import DscopeError


def test_repeat_is_off_by_default_and_that_is_what_hal_config_says():
    st = scope.Settings()
    assert st.repeat == 0
    hal, _, _, _ = scope.hal_config(st)
    assert hal['repeat'] == 0


def test_a_repeat_setting_reaches_the_hal_and_the_geometry():
    st = scope.Settings(repeat=8)
    hal, _, _, _ = scope.hal_config(st)
    assert hal['repeat'] == 1
    # the geometry has to switch too: repeat transfers carry a 4 KiB tail per frame
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['repeat'] is True
    assert geom['pkt_bytes'] > 0 and geom['pkt_bytes'] % 1024 == 0


@pytest.mark.parametrize('trigger, mode', [(None, 'force'), ('A0', 'force'), (None, 'auto')])
def test_repeat_accepts_a_forced_or_untriggered_start(trigger, mode):
    st = scope.Settings(repeat=3, trig_source=trigger, trig_mode=mode)
    assert scope.repeat_plan(st) == 3


def test_repeat_refuses_a_triggered_start_instead_of_silently_ignoring_the_trigger():
    st = scope.Settings(repeat=3, trig_source='A0', trig_mode='auto')
    with pytest.raises(DscopeError) as e:
        scope.repeat_plan(st)
    assert e.value.code == 'INVALID_ARG'
    assert 'force' in (e.value.hint or '')


def test_a_negative_frame_count_is_refused():
    with pytest.raises(DscopeError) as e:
        scope.repeat_plan(scope.Settings(repeat=-1))
    assert e.value.code == 'INVALID_ARG'


def test_off_still_plans_a_single_capture():
    assert scope.repeat_plan(scope.Settings()) == 0


def test_the_cli_flag_reaches_the_settings():
    from openm454.cli import build_parser
    a = build_parser().parse_args(['capture', '--repeat', '5'])
    st = scope.settings_from_args(a)
    assert st.repeat == 5


def test_the_default_command_line_still_plans_a_single_capture():
    from openm454.cli import build_parser
    a = build_parser().parse_args(['capture'])
    assert scope.repeat_plan(scope.settings_from_args(a)) == 0
