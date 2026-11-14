"""Scope-level paths around the engine (no hardware): completeness checks, trigger domain, timeouts, budgets."""
import types

import pytest

from openm454 import scope as scopemod
from openm454.acquire import RawCapture
from openm454.errors import DscopeError


class FakeDev:
    def __init__(self, speed='super'):
        self.zero_info = scopemod._cached_zero()
        self.info = {'usb_speed': speed}
        self.t = types.SimpleNamespace(running=False, stale=False)


class OneShotEngine:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def run(self, prog, **kw):
        self.calls.append(kw)
        return self.raw


def scope_with(raw, speed='super'):
    s = scopemod.Scope()
    s.dev = FakeDev(speed)
    s.engine = OneShotEngine(raw)
    return s


def head(triggered=True):
    h = bytearray(4096)
    h[:4] = b'\x55\x55\x55\x55'
    if not triggered:
        h[8:12] = (0x80000000).to_bytes(4, 'little')
    return bytes(h)


def st_a1(**kw):
    st = scopemod.Settings()
    st.analog, st.digital = ['A1'], []
    st.trig_source, st.trig_mode = 'A1', 'normal'
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_incomplete_record_is_an_error_not_ok():
    raw = RawCapture()
    raw.head, raw.data, raw.timed_out = head(), bytearray(1000), True
    s = scope_with(raw)
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1())
    assert ei.value.code == 'TIMEOUT_DATA' and ei.value.exit_code == 6


def test_aborted_before_trigger_is_not_reported_as_no_trigger():
    raw = RawCapture()
    raw.aborted = True
    with pytest.raises(DscopeError) as ei:
        scope_with(raw).capture(st_a1())
    assert ei.value.code == 'TIMEOUT_WATCHDOG'


def test_untriggered_capture_uses_a_domain_with_channels():
    for an, dg, want in ((['A1'], [], (1, 1)), (['A2', 'A3'], ['D0'], (1, 2)), ([], ['D5', 'D7'], (0, 5))):
        st = scopemod.Settings()
        st.analog, st.digital, st.trig_source, st.trig_mode = an, dg, None, 'force'
        hal = scopemod.hal_config(st)[0]
        assert (hal['trigger']['ch_type'], hal['trigger']['ch_index']) == want


def test_trigger_wait_includes_the_record_time():
    raw = RawCapture()
    s = scope_with(raw)
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1(duration=1.0, timeout=0.5))
    assert ei.value.code == 'TIMEOUT_NO_TRIGGER'
    waited = s.engine.calls[0]['trigger_timeout']
    assert waited >= 0.5 + 1.0                   # the head of a 1 s non-roll record cannot come sooner


def test_bulk_reads_are_padded_to_the_endpoint_max_packet():
    s = scope_with(RawCapture())
    st = st_a1()
    want = s.plan(st)['geometry']['pkt_bytes']
    assert want % 1024                                   # the geometry really produces an unaligned read
    with pytest.raises(DscopeError):
        s.capture(st)
    assert s.engine.calls[0]['pkt_bytes'] == (want + 1023) // 1024 * 1024


def test_usb2_link_is_refused_before_any_run():
    s = scope_with(RawCapture(), speed='high')
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1())
    assert ei.value.code == 'UNSUPPORTED' and s.engine.calls == []


def test_out_of_range_settings_are_invalid_arg():
    s = scopemod.Scope()
    with pytest.raises(DscopeError) as ei:
        s.plan(st_a1(duration=1000.0))
    assert ei.value.code == 'INVALID_ARG'


def test_stream_defaults_avoid_small_packet_mode():
    from openm454 import cli
    a = cli.build_parser().parse_args(['stream', '-c', 'A1', '--duration', '10s'])
    geom = scopemod.Scope().plan(cli.stream_settings(a))['geometry']
    assert not geom['small_pkt'] and geom['need_bytes'] == geom['acqu_bytes'] + 4096


def test_offline_plan_works_without_the_calibration_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(scopemod, 'STATE', str(tmp_path))
    plan = scopemod.Scope().plan(st_a1())
    assert plan['geometry']['record_s'] > 0


def test_zero_duration_is_invalid_arg():
    with pytest.raises(DscopeError) as ei:
        scopemod.hal_config(st_a1(duration=0.0))
    assert ei.value.code == 'INVALID_ARG'


def test_stream_user_stop_before_data_is_reported_as_stopped():
    raw = RawCapture()
    raw.aborted = raw.stopped = True
    s = scope_with(raw)
    st = scopemod.Settings()
    st.analog, st.digital, st.duration = ['A1'], [], 1.0
    with pytest.raises(DscopeError) as ei:
        s.stream(st)
    assert ei.value.code == 'STOPPED' and ei.value.exit_code == 0


def test_cli_output_has_one_owner_after_the_watchdog_fires(capsys):
    from openm454 import cli
    cli._emit({'a': 1})
    cli._OUT['owner'] = 'watchdog'
    try:
        cli._emit({'b': 2})                          # main thread: suppressed
        cli._emit({'c': 3}, owner='watchdog')
    finally:
        cli._OUT['owner'] = None
    out = capsys.readouterr().out.splitlines()
    assert out == ['{"a":1}', '{"c":3}']


def test_watchdog_exits_although_stdout_is_blocked(monkeypatch):
    """A reader that stops draining stdout leaves the main thread inside _emit holding the output lock; the
    watchdog must still stop the scope and exit (hot-unplug test, 2026-10-31)."""
    import threading
    import time
    from openm454 import cli
    exited, stopped = threading.Event(), threading.Event()

    class ExitCalled(BaseException):
        pass

    def fake_exit(code):
        exited.code = code
        exited.set()
        raise ExitCalled

    def fire():
        try:
            cli._watchdog_fire('stream', 10.0, types.SimpleNamespace(emergency_stop=stopped.set),
                               threading.Event(), wait=0.2)
        except ExitCalled:
            pass

    monkeypatch.setattr(cli.os, '_exit', fake_exit)
    cli._OUT['lock'].acquire()                       # the stuck main thread
    try:
        t0 = time.time()
        th = threading.Thread(target=fire, daemon=True)
        th.start()
        assert exited.wait(5.0) and exited.code == 6 and stopped.is_set()
        assert time.time() - t0 < 2.0
    finally:
        cli._OUT['lock'].release()
        th.join(2.0)
        cli._OUT['owner'] = None


def test_watch_measures_only_the_channels_its_output_needs(capsys):
    """Deep records measure at ~4 s per 100M-sample channel: with --fields, watch measures only the named channels
    (plus those in --stop-when)."""
    from openm454 import cli
    asked = []

    class Cap:
        meta = {'acq': {'triggered': True}}

        def summary(self, channels=None):
            asked.append(channels)
            return {'analog': {'A1': {'vpp_v': 2.9}}, 'digital': {'D0': {'edges': 4}}}

    scope = types.SimpleNamespace(capture=lambda st: Cap())
    args = cli.build_parser().parse_args(['watch', '-c', 'A0-A3,D0', '--fields', 'A1.vpp_v', '--count', '1',
                                          '--stop-when', "D0['edges'] > 0"])
    cli.cmd_watch(args, scope)
    assert asked == [{'A1', 'D0'}]
    args = cli.build_parser().parse_args(['watch', '-c', 'A1', '--count', '1'])
    cli.cmd_watch(args, scope)
    assert asked[-1] is None                         # default fields: every channel
    capsys.readouterr()


def test_explicit_max_wall_only_extends_the_budget():
    from openm454 import cli
    a = cli.build_parser().parse_args(['--max-wall', '5s', 'capture', '-c', 'A1'])
    computed, _ = cli._budget(a)
    assert computed >= 60.0                          # capture keeps a 60 s floor; main() uses max(given, computed)


def test_window_is_kept_when_the_rate_snaps_down():
    """1 analog channel runs at 4 x the table rates, so 100 MSa/s does not exist: 5 ms with 500k samples becomes
    80 MSa/s and 400k samples (a 5 ms window), not 500k samples over 6.25 ms (seen on hardware 2026-10-31)."""
    for an, samples, want_rate in ((['A1'], 500_000, 80e6), (['A1', 'A3'], 500_000, 100e6), (['A1'], 2_000_000, 400e6),
                                   (['A0', 'A1', 'A2', 'A3'], 500_000, 100e6), (['A1'], 5_000, 800e3)):
        st = st_a1(analog=an, samples=samples, duration=5e-3)
        geom = scopemod.Scope().plan(st)['geometry']
        assert geom['analog_rate'] == want_rate
        assert abs(geom['record_s'] - 5e-3) / 5e-3 < 0.03, (an, samples, geom['record_s'])   # 64-sample blocks


def test_digital_pulse_trigger_needs_no_level():
    from openm454 import cli
    from openm454.scope import settings_from_args
    st = settings_from_args(cli.build_parser().parse_args(['capture', '-c', 'D4', '--trig', 'D4:npulse>:200us']))
    assert (st.trig_source, st.trig_edge, st.trig_level) == ('D4', 'npulse>', None)
    assert abs(st.trig_pulse - 200e-6) < 1e-12


class RefusingEngine:
    """The USB_NO_MEM precheck of openm454.acquire: refuses before the setup program writes anything."""

    def __init__(self):
        self.calls = []

    def run(self, prog, **kw):
        self.calls.append(kw)
        raise DscopeError('USB_NO_MEM', 'refusing this capture before the setup program', 'raise usbfs_memory_mb')


def geom(**kw):
    g = {'pkt_bytes': 16384, 'need_bytes': 65536, 'transfer_count': 4, 'analog_rate': 100e6, 'record_s': 5e-3,
         'repeat': False}
    g.update(kw)
    return g


def test_a_refused_run_leaves_the_front_end_state_alone():
    """Nothing was written and nothing was waited for, so the next run must still pay the wake and overdrive waits."""
    s = scope_with(None)
    s.engine = RefusingEngine()
    s._analog_awake, s._overdriven, s._last_cross = False, True, [(0x44, 1)]
    with pytest.raises(DscopeError) as ei:
        s._run([(0x44, 1), (0x10, 0x1234)], geom(), 1.0, None)
    assert ei.value.code == 'USB_NO_MEM'
    assert s._analog_awake is False and s._overdriven is True     # untouched: no analog wake, overdrive unknown
    assert s._last_cross is None                                  # write progress unknown: rewrite the tables


def test_a_finished_run_commits_the_front_end_state():
    raw = RawCapture()
    raw.head = head()
    s = scope_with(raw)
    s._analog_awake, s._overdriven = False, True
    prog = [(0x44, 1), (0x10, 0x1234)]
    s._run(prog, geom(), 1.0, None)
    assert s.engine.calls[0]['settle'] == max(scopemod.OVERDRIVE_SETTLE, scopemod.ANALOG_WAKE_SETTLE)
    assert s._analog_awake is True and s._overdriven is False
    assert s._last_cross == [(0x44, 1)]                           # written: the next run may skip them
    s._run(prog, geom(), 1.0, None)
    assert s.engine.calls[1]['settle'] == 0.0                     # awake and not overdriven: no wait


class FlakyEngine(OneShotEngine):
    """OneShotEngine that raises `fail` on the next run when it is set (once)."""

    def __init__(self, raw):
        super().__init__(raw)
        self.fail = None

    def run(self, prog, **kw):
        self.calls.append(kw)
        if self.fail is not None:
            e, self.fail = self.fail, None
            raise e
        return self.raw


@pytest.mark.parametrize('error', [KeyboardInterrupt(), DscopeError('USB_ERROR', 'submit failed', 'replug'),
                                   DscopeError('USB_NO_MEM', 'libusb NO_MEM while queueing', 'usbfs_memory_mb')],
                         ids=['ctrl_c', 'usb_error', 'usb_no_mem_at_submit'])
def test_a_failed_digital_only_run_makes_the_next_analog_run_wait(error):
    """These failures come after the setup program, and a digital-only program idles the analog front end: the next
    analog run must still wait ANALOG_WAKE_SETTLE (it skipped the wait before the fix)."""
    raw = RawCapture()
    raw.head = head()
    s = scope_with(raw)
    s.engine = FlakyEngine(raw)
    prog = [(0x44, 1), (0x10, 0x1234)]
    s._run(prog, geom(), 1.0, None)
    assert s._analog_awake is True
    s.engine.fail = error
    with pytest.raises(type(error)):
        s._run([(0x10, 0x1234)], geom(analog_rate=None), 1.0, None)
    assert s._analog_awake is False
    s._run(prog, geom(), 1.0, None)
    assert s.engine.calls[-1]['settle'] == scopemod.ANALOG_WAKE_SETTLE


def test_a_failed_analog_run_leaves_an_awake_front_end_awake():
    raw = RawCapture()
    raw.head = head()
    s = scope_with(raw)
    s.engine = FlakyEngine(raw)
    s._analog_awake = True
    s.engine.fail = DscopeError('USB_ERROR', 'submit failed', 'replug')
    with pytest.raises(DscopeError):
        s._run([(0x44, 1), (0x10, 0x1234)], geom(), 1.0, None)
    assert s._analog_awake is True


@pytest.mark.parametrize('limit', [None, 16], ids=['no_usbfs', 'usbfs_16MiB'])
def test_geometry_to_engine_queue_depth(monkeypatch, limit):
    """12 channels at 100 MSa/s, 5 ms through Scope.capture and the real Engine on the simulator: the geometry asks
    for 16 reads of 1048320 B (16 of those + the head fill 16 MiB exactly), the engine gets them padded to 1 MiB and
    queues what the usbfs budget allows; without usbfs (Windows, macOS) it keeps the geometry's 16."""
    from openm454 import acquire as acq
    from openm454 import cli
    from test_acquire_sim import FakeDevice, Sim
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: limit)
    st = scopemod.settings_from_args(cli.build_parser().parse_args(
        ['capture', '-c', 'A0-A3,D0-D7', '--rate', '100M', '--duration', '5ms', '--trig-mode', 'force']))
    s = scopemod.Scope()
    s.dev = FakeDev()
    g = s.plan(st)['geometry']
    assert (g['pkt_bytes'], g['transfer_count']) == (1048320, 16)
    sim = Sim()
    eng = acq.Engine(FakeDevice(sim))
    seen = []
    s.engine = types.SimpleNamespace(run=lambda prog, **kw: seen.append(kw) or eng.run(prog, **kw))
    cap = s.capture(st)
    assert (seen[0]['pkt_bytes'], seen[0]['n_transfers']) == (1 << 20, 16)
    want = acq._transfers_for(limit, 1 << 20, 16)
    if limit is None:
        assert want == 16
    else:
        assert acq.MIN_TRANSFERS <= want < 16
    q = cap.meta['acq']
    assert q['transfers'] == want and len(eng._trs) == want + 1          # head + the data reads
    assert q.get('transfers_wanted') == (16 if want < 16 else None)      # named only when lowered
    assert q['complete'] and q['stop'] == 'normal' and sim.violations == []
