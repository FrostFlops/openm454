"""Loop-mode logging (openm454/logger.py) and the refreshing stream, without hardware."""
import json
import math
import os
import types

import numpy as np
import pytest

from openm454 import cli, host, logger, mso_adapter, packet
from openm454 import scope as scopemod
from openm454.acquire import RawCapture
from openm454.capture import load_capture
from openm454.errors import DscopeError
from openm454.units import si

A2D8 = (['A2', 'A3'], ['D%d' % i for i in range(8)])


@pytest.fixture(autouse=True)
def no_host_file(tmp_path, monkeypatch):
    """Tests never read or write the real state/host.json (bench results and allowances) or state/stop.request."""
    monkeypatch.setattr(host, 'HOST_FILE', str(tmp_path / 'host.json'))
    monkeypatch.setattr(cli, 'STOP_REQUEST', str(tmp_path / 'stop.request'))


def settings(analog, digital):
    st = scopemod.Settings()
    st.analog, st.digital = list(analog), list(digital)
    st.vdiv = {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')}
    st.offset = {c: 1.5 for c in ('A0', 'A1', 'A2', 'A3')}
    return st


def make_plan(analog, digital, rate, duration, tmp_path=None, **kw):
    kw.setdefault('storage', 'ram' if tmp_path is not None else 'none')
    return logger.plan(settings(analog, digital), rate, duration, out_dir=str(tmp_path) if tmp_path else None,
                       zero_info=scopemod._cached_zero(), **kw)


# ------------------------------------------------------------------------------------------- plan
def test_loop_program_matches_the_hardware_verified_one():
    pl = make_plan(['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)], 50e3, 10)
    regs = dict(pl['_prog'])
    assert regs[0x00] == 6                                        # HW_BASE_MODE: stream + loop
    assert (regs[0x13] | regs[0x14] << 16) + 1 == 1280            # HORI_RWORDS = one 5120-byte packet
    assert regs[0x28] == 0x400 and regs[0x20] == 0x800            # folds 0: 0x100 x channels
    assert pl['packet_bytes'] == 5120 and pl['transfers_planned'] == 10 and pl['_geom']['need_bytes'] is None
    assert 'transfers' not in pl                                  # the run's acq.transfers is the depth actually used
    lay = packet.layout_from_regs(pl['_geom']['_regs'], rates={'analog': 50e3, 'digital': 50e3})
    assert lay.loop and lay.acq_words is None and lay.block_bytes == 320


A4D8 = (['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)])


def test_verdicts_and_gapfree_limit():
    assert make_plan(*A2D8, 50e6, 10)['verdict'] == 'sustained'           # 150 MB/s, verified 3 min
    near = make_plan(['A0', 'A1', 'A2'], ['D0', 'D1'], 50e6, 10)                # 162.5 MB/s
    assert near['verdict'] == 'near_limit' and near['warnings']
    burst = make_plan(*A2D8, 100e6, 1)                                     # 300 MB/s for 1 s
    # a log of T s ends when the host has read 300 MB/s x T at R = 190 MB/s (assumed: not measured yet); the
    # backlog then, (D - R) x D x T / R, must stay below the 480 MiB guard
    t_log = 480 * 2 ** 20 * 190e6 / (300e6 * 110e6)
    assert burst['verdict'] == 'burst' and burst['gapfree_max_s'] == pytest.approx(t_log, abs=0.01)
    assert burst['recommendation'] == 'log' and burst['backlog_growth_mb_s'] == 110.0 and not burst['provisional']
    assert 'assumed' in burst['readout_source']


def test_longer_than_gapfree_needs_force_when_log_gives_the_longest_piece():
    # 300 MB/s: one gap-free piece of ~2.9 s beats stream's records of 512 MiB / 300 MB/s = 1.79 s
    with pytest.raises(DscopeError) as ei:
        make_plan(*A2D8, 100e6, 10)
    e = ei.value
    assert e.code == 'NEEDS_FORCE' and e.exit_code == 2 and '--force' in e.hint and 'stream' in e.hint
    assert e.plan['recommendation'] == 'log --force' and e.plan['gapfree_max_s'] == pytest.approx(2.9, abs=0.01)
    alt = e.plan['stream_alternative']                                   # stream runs the digital side at 50M only
    assert not alt['same_rates'] and alt['digital_rate_hz'] == 50e6 and 'digital at 50M' in alt['summary']
    assert e.to_dict()['plan'] == e.plan                                  # the CLI prints the numbers
    with pytest.raises(DscopeError) as ei:
        make_plan(*A2D8, 100e6, None)                                      # 'forever' too
    assert ei.value.code == 'NEEDS_FORCE'
    forced = make_plan(*A2D8, 100e6, None, force=True)
    assert forced['verdict'] == 'burst' and 'ring_full' in ' '.join(forced['warnings'])
    assert forced['recommendation'] == 'log --force' and forced['_force']


def test_stream_better_is_refused():
    # 4 analog + 8 digital at 100 MSa/s = 500 MB/s: gap-free 0.67 s at R = 200, stream records 1.07 s
    host.readout_put(host.readout_key(4, 8, 100e6), 200.0, False, 500.0)
    with pytest.raises(DscopeError) as ei:
        make_plan(*A4D8, 100e6, 10)
    e = ei.value
    assert e.code == 'STREAM_BETTER' and 'openm454 stream' in e.hint and e.plan['recommendation'] == 'stream'
    args = cli.build_parser().parse_args(['stream', '-c', 'A0-A3,D0-D7', '--rate', '100M', '--duration', '10s'])
    assert e.plan['stream_alternative']['record_s'] == pytest.approx(cli.stream_plan(args)[2], rel=1e-3)   # 1.02 s
    assert e.plan['stream_alternative']['same_rates'] and 'measured' in e.plan['readout_source']
    with pytest.raises(DscopeError) as ei:                                 # longer than gap-free, fits one record
        make_plan(*A4D8, 100e6, 0.9)
    one = ei.value.plan['stream_alternative']
    assert ei.value.code == 'STREAM_BETTER' and one['records'] == 1 and one['coverage_pct'] == 100.0
    assert make_plan(*A4D8, 100e6, 0.5)['recommendation'] == 'log'       # within the gap-free limit: log
    forced = make_plan(*A4D8, 100e6, 10, force=True)                       # forced anyway: says so
    assert any('stream would do better' in w for w in forced['warnings'])


def test_measured_readout_replaces_the_assumed_one():
    host.readout_put(host.readout_key(2, 8, 100e6), 266.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, 10)                                       # 13 s gap-free at 266 MB/s: fine
    assert pl['readout_mb_s'] == 266.0 and pl['gapfree_max_s'] == pytest.approx(
        480 * 2 ** 20 * 266e6 / (300e6 * 34e6), abs=0.01) and pl['recommendation'] == 'log'
    host.readout_put(host.readout_key(3, 2, 50e6), 162.5, True, 162.5)   # the host kept up: a lower bound
    assert make_plan(['A0', 'A1', 'A2'], ['D0', 'D1'], 50e6, 10)['verdict'] == 'sustained'
    host.readout_put(host.readout_key(3, 5, 50e6), 181.2, True, 181.25)  # stored rounded below D: still kept up
    assert make_plan(['A0', 'A1', 'A2'], ['D%d' % i for i in range(5)], 50e6, None)['verdict'] == 'sustained'


def test_unmeasured_readout_is_deferred_to_a_probe():
    pl = make_plan(*A2D8, 100e6, 10, defer=True)                           # no refusal yet: R is measured first
    assert pl['provisional'] and pl['_needs_probe'] and 'probe' in pl['readout_source']
    assert pl['recommendation'] == 'log --force'
    assert not make_plan(*A2D8, 1e6, 10, defer=True)['_needs_probe']      # 3 MB/s needs no probe
    host.readout_put(host.readout_key(2, 8, 100e6), 266.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, 10, defer=True)
    assert not pl['provisional'] and not pl['_needs_probe']


def test_rates():
    with pytest.raises(DscopeError):
        make_plan(['A2'], [], 800e6, 1)                                    # 400 MSa/s is the top (interleaved)
    pl = make_plan(['A2'], [], 33e6, 1)                                    # 100 MHz / 3 (any integer divider)
    assert pl['divider'] == 3 and pl['rate_hz'] == pytest.approx(100e6 / 3) and '100 MHz / 3' in pl['warnings'][0]
    regs = dict(pl['_prog'])
    assert regs[0x15] == 3 and regs[0x16] == 0                             # not ceil(100M / 33333333) = 4
    pl = make_plan(['A2'], [], 1e3, 1)
    assert pl['divider'] == 100_000 and dict(pl['_prog'])[0x15] == 100_000 & 0xFFFF and dict(pl['_prog'])[0x16] == 1
    with pytest.raises(DscopeError):
        make_plan(['A2'], [], 500, 1)


def test_ram_is_the_default_and_must_fit(tmp_path):
    big = 1 << 30
    pl = make_plan(*A2D8, 1e6, 60, tmp_path, ram_budget=big)              # 180 MB -> RAM, one file at the end
    assert pl['storage'] == 'ram' and not pl['ram_ring'] and pl['file_mb'] == 180.0 and pl['max_duration_s'] == 60
    with pytest.raises(DscopeError) as ei:
        make_plan(*A2D8, 10e6, 60, tmp_path, ram_budget=big)              # 1.8 GB does not fit 1 GB
    assert 'RAM budget' in ei.value.message and '--keep-last' in ei.value.hint
    ring = make_plan(*A2D8, 10e6, 60, tmp_path, ram_budget=big, keep_last=True)
    assert ring['ram_ring'] and ring['ram_window_s'] == pytest.approx(big / 30e6, rel=1e-3)
    forever = make_plan(*A2D8, 1e6, None, tmp_path, ram_budget=big)
    assert forever['ram_ring'] and forever['max_duration_s'] is None and forever['file_mb'] == pytest.approx(big / 1e6, rel=1e-3)


def test_disk_is_explicit_and_warns_about_wear(tmp_path, monkeypatch):
    pl = make_plan(*A2D8, 10e6, 60, tmp_path, storage='disk')              # 1.8 GB in ~256 MB chunks
    assert pl['storage'] == 'disk' and 200 <= pl['chunk_mb'] <= 300
    assert pl['chunk_mb'] == pytest.approx(pl['chunk_s'] * 30, rel=1e-3) and pl['chunks'] == -(-60 // pl['chunk_s'])
    assert not any('SSD' in w for w in pl['warnings'])
    long = make_plan(*A2D8, 50e6, None, tmp_path, storage='disk', disk_budget=1e12)    # 540 GB per hour
    assert any('SSDs' in w for w in long['warnings'])
    with pytest.raises(DscopeError) as ei:
        make_plan(*A2D8, 1e6, 60, tmp_path, storage='disk', disk_budget=100e6)          # allowance 100 MB < 180 MB
    assert 'allowance' in ei.value.message
    monkeypatch.setattr(logger.shutil, 'disk_usage', lambda d: types.SimpleNamespace(free=100e6))
    with pytest.raises(DscopeError):
        make_plan(*A2D8, 1e6, 60, tmp_path, storage='disk')               # 180 MB > 90 % of 100 MB free
    with pytest.raises(DscopeError):
        make_plan(*A2D8, 1e6, 60, tmp_path, ram_budget=1 << 30)           # even the RAM log's final file


def test_owner_allowance_sets_the_default_budget(tmp_path, monkeypatch):
    host.save({'allow': {'ram_bytes': 2e9, 'disk_bytes': 5e9}})
    monkeypatch.setattr(logger, 'available_ram', lambda: 16e9)
    assert logger.default_ram_budget() == 2e9
    monkeypatch.setattr(logger, 'available_ram', lambda: 1e9)
    assert logger.default_ram_budget() == 0.8e9                          # never more than 80 % of what is free
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk')
    assert pl['max_duration_s'] == pytest.approx(5e9 / 3e6, rel=1e-3)


def test_small_packets_are_planned_not_refused():
    pl = make_plan(['A2'], [], 20e3, 5)
    assert pl['small_packets'] and pl['packet_bytes'] == 4096 and pl['usb_mb_s'] == pytest.approx(1.28)


def test_long_durations_parse():
    assert si('2h', 's') == 7200 and si('5min', 's') == 300 and si('1d', 's') == 86400 and si('10ms', 's') == 0.01


# ------------------------------------------------------------------------------------------- run (fake device)
def encode(layout, analog, digital):
    """Inverse of packet._decode_blocks: per-channel codes / bits -> the block stream."""
    n = len(next(iter(analog.values()))) if analog else len(next(iter(digital.values())))
    nb = n // 64
    B = np.zeros((nb, layout.block_bytes), np.uint8)
    ab = layout.analog_block_bytes
    if layout.analog:
        A = B[:, :ab].reshape(nb, 8, len(layout.analog), 8)
        for i, ch in enumerate(layout.analog):
            A[:, :, i, :] = analog[ch][:nb * 64].reshape(nb, 8, 8)
    if layout.digital:
        D = B[:, ab:].reshape(nb, 1, len(layout.digital), 8)
        for i, ch in enumerate(layout.digital):
            D[:, 0, i, :] = np.packbits(digital[ch][:nb * 64].reshape(nb, 64), axis=1, bitorder='little')
    return B.tobytes()


def square(n, period, lo=60, hi=200):
    return np.where((np.arange(n) % period) < period // 2, hi, lo).astype(np.uint8)


class FakeScope:
    """Feeds a synthetic loop stream through on_chunk in transfers of the planned packet size."""

    def __init__(self, stream, pkt, small_blk=None, clock=None, max_transfers=None, pass_len=1):
        self.stream, self.pkt, self.small_blk = stream, pkt, small_blk
        self.clock, self.max_transfers, self.pass_len = clock, max_transfers, pass_len
        self.calls = []
        self.delivered = 0

    def _run(self, prog, geom, trigger_timeout, collect_timeout, **kw):
        self.calls.append((geom, collect_timeout, kw))
        raw = RawCapture()
        head = bytearray(4096)
        head[:4] = b'\x55\x55\x55\x55'
        head[8:12] = (0xC0000007).to_bytes(4, 'little')
        raw.head = bytes(head)
        kw['on_chunk'](raw.head)
        step = self.small_blk or self.pkt
        pos, k = 0, 0
        while True:
            if k % self.pass_len == 0 and kw['stop_flag']():
                raw.stopped = raw.aborted = True
                break
            if self.max_transfers is not None and k >= self.max_transfers:
                break
            d = self.stream[pos % len(self.stream):][:step]
            if len(d) < step:
                d = d + self.stream[:step - len(d)]
            pos += step
            k += 1
            if self.small_blk:
                d = d + bytes(4096 - len(d))                  # one block, then filler
            if self.clock:
                self.clock.tick()
            self.delivered += len(d)
            kw['on_chunk'](d)
            assert not kw.get('keep_data', True)
        raw.stop = 'normal'
        return raw


def fake_signal(pl, seconds):
    rate = pl['rate_hz']
    layout = packet.layout_from_regs(pl['_geom']['_regs'], rates={'analog': rate, 'digital': rate})
    n = int(round(seconds * rate / 64)) * 64
    an = {ch: square(n, 100 + 2 * ch) for ch in layout.analog}
    dg = {ch: (square(n, 50 + ch) > 100).astype(np.uint8) for ch in layout.digital}
    return layout, an, dg, encode(layout, an, dg)


def test_chunked_log_round_trip(tmp_path):
    pl = make_plan(*A2D8, 50e3, 3.0, tmp_path, storage='disk', chunk_s=1.0, every=0.5)
    layout, an, dg, stream = fake_signal(pl, 4.0)
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'x'), emit=lines.append)
    assert res['ok'] and res['acq']['stop_reason'] == 'duration'
    n = 150_016                                          # 3 s rounded up to whole 64-sample blocks
    assert res['acq']['samples'] == n and len(lines) == 6
    idx = json.load(open(res['files']['index']))
    assert idx['status'] == 'done' and [c['samples'] for c in idx['chunks']] == [50_048, 50_048, 49_920]
    caps = [load_capture(os.path.join(res['files']['dir'], c['file'])) for c in idx['chunks']]
    assert caps[1].meta['t0_analog_s'] == pytest.approx(50_048 / 50e3)
    a2 = np.concatenate([c.analog['A2'] for c in caps])
    np.testing.assert_array_equal(a2, an[2][:n])        # chunks join without a gap
    d5 = np.concatenate([c.digital['D5'] for c in caps])
    np.testing.assert_array_equal(d5[7:], dg[5][7:n])   # the first 7 digital samples are back-filled (stale)
    assert (d5[:7] == dg[5][7]).all()
    edges = sum(l['D5']['edges'] for l in lines)
    assert edges == np.count_nonzero(np.diff(d5.astype(np.int8)))
    assert res['digital']['D5']['state'] == 'toggling' and 'A2' in res['analog']


def test_memory_log_is_one_capture(tmp_path):
    pl = make_plan(['A2'], ['D0', 'D1'], 100e3, 1.0, tmp_path)
    assert pl['storage'] == 'ram'
    layout, an, dg, stream = fake_signal(pl, 1.5)
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(['A2'], ['D0', 'D1']), pl, str(tmp_path / 'm'))
    cap = load_capture(res['files']['npz'])
    assert len(cap.analog['A2']) == 100_032 and cap.meta['acq']['mode'] == 'loop'
    assert cap.measure('A2')['freq_hz'] == pytest.approx(100e3 / 104, rel=1e-3)


def test_small_packets_take_one_block_per_read(tmp_path):
    pl = make_plan(['A2'], [], 20e3, 0.5, tmp_path)
    layout, an, dg, stream = fake_signal(pl, 1.0)
    fs = FakeScope(stream, 4096, small_blk=layout.block_bytes)
    res = logger.run(fs, settings(['A2'], []), pl, str(tmp_path / 's'))
    cap = load_capture(res['files']['npz'])
    np.testing.assert_array_equal(cap.analog['A2'], an[2][:len(cap.analog['A2'])])
    assert fs.calls[0][0]['transfer_count'] >= 4 and fs.calls[0][1] is None
    # the plan reports the depth the run asks for, not DSWave's 1 read of one block
    assert pl['small_packets'] and pl['_geom']['transfer_count'] < logger.SMALL_PKT_TRANSFERS
    assert pl['transfers_planned'] == fs.calls[0][0]['transfer_count'] == logger.SMALL_PKT_TRANSFERS


class DepthScope(FakeScope):
    """FakeScope whose engine queued `got` reads (Linux: a usbfs budget may lower the planned depth)."""

    def __init__(self, *a, got=None, **kw):
        super().__init__(*a, **kw)
        self.got = got

    def _run(self, prog, geom, *a, **kw):
        raw = super()._run(prog, geom, *a, **kw)
        raw.transfers_wanted = geom['transfer_count']
        raw.transfers = self.got if self.got is not None else geom['transfer_count']
        return raw


def test_log_result_reports_the_queue_depth_it_used(tmp_path):
    pl = make_plan(['A2'], ['D0', 'D1'], 100e3, 0.5)
    stream = fake_signal(pl, 1.0)[3]
    res = logger.run(DepthScope(stream, pl['packet_bytes']), settings(['A2'], ['D0', 'D1']), pl)
    assert res['acq']['transfers'] == pl['transfers_planned'] and 'transfers_wanted' not in res['acq']
    want = pl['transfers_planned']
    res = logger.run(DepthScope(stream, pl['packet_bytes'], got=want - 2), settings(['A2'], ['D0', 'D1']), pl)
    assert (res['acq']['transfers'], res['acq']['transfers_wanted']) == (want - 2, want)
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(['A2'], ['D0', 'D1']), pl)
    assert res['acq']['transfers'] is None                       # an engine that does not say: no number, no error


class Clock:
    def __init__(self, step):
        self.t, self.step = 1000.0, step

    def tick(self):
        self.t += self.step

    def __call__(self):
        return self.t


def test_backlog_stops_before_the_ring_could_overwrite(tmp_path, monkeypatch):
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk', chunk_s=1.0)
    layout, an, dg, stream = fake_signal(pl, 1.0)
    pkt_s = pl['packet_bytes'] / pl['_data_bps']
    clock = Clock(3 * pkt_s)                             # the host reads one packet per three packet times
    monkeypatch.setattr(logger.time, 'perf_counter', clock)
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 20e6)
    res = logger.run(FakeScope(stream, pl['packet_bytes'], clock=clock), settings(*A2D8), pl, str(tmp_path / 'b'))
    assert not res['ok'] and res['error']['code'] == 'HOST_TOO_SLOW' and res['acq']['stop_reason'] == 'backlog'
    assert 20 <= res['acq']['backlog_max_mb'] < 25
    idx = json.load(open(res['files']['index']))
    assert idx['status'] == 'error' and idx['chunks']           # what arrived before the stop is kept


def test_user_stop_and_no_save(tmp_path):
    pl = make_plan(*A2D8, 1e6, None)
    layout, an, dg, stream = fake_signal(pl, 0.2)
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 40
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, emit=lines.append,
                     stop_flag=stop)
    assert res['ok'] and res['acq']['stop_reason'] == 'stop_request' and res['files'] == {} and lines


def test_stalled_stream_is_an_error(tmp_path, monkeypatch):
    pl = make_plan(['A2'], [], 1e6, None)
    layout, an, dg, stream = fake_signal(pl, 0.1)
    clock = Clock(0.0)
    monkeypatch.setattr(logger.time, 'perf_counter', clock)

    class Stall(FakeScope):
        def _run(self, prog, geom, *a, **kw):
            raw = RawCapture()
            raw.head = b'\x55\x55\x55\x55' + bytes(4092)
            kw['on_chunk'](raw.head)
            kw['on_chunk'](self.stream[:geom['pkt_bytes']])
            clock.t += 10.0                               # nothing more arrives
            assert kw['stop_flag']()
            raw.stop = 'normal'
            return raw
    res = logger.run(Stall(stream, pl['packet_bytes']), settings(['A2'], []), pl, None)
    assert res['error']['code'] == 'TIMEOUT_DATA'


def test_cli_log_plan_needs_no_device(capsys):
    assert cli.main(['log', '--plan', '-c', 'A2,A3,D0-D7', '--rate', '50M', '--duration', '1h', '--no-save']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['plan']['verdict'] == 'sustained' and out['plan']['data_mb_s'] == 150.0
    # 400 MB/s for 1 h: provisional until this channel set's readout is measured ...
    assert cli.main(['log', '--plan', '-c', 'A0-A3', '--rate', '100M', '--duration', '1h', '--no-save']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['plan']['provisional'] and out['plan']['recommendation'] == 'stream'
    # ... then decided: 275 MB/s gives 2.8 s gap-free, longer than stream's 1.34 s records
    host.readout_put(host.readout_key(4, 0, 100e6), 275.0, False, 400.0)
    assert cli.main(['log', '--plan', '-c', 'A0-A3', '--rate', '100M', '--duration', '1h', '--no-save']) == 2
    out = json.loads(capsys.readouterr().out)
    assert out['error']['code'] == 'NEEDS_FORCE'
    assert out['error']['plan']['gapfree_max_s'] == 2.76


# ------------------------------------------------------------------------------------------- refreshing stream
class FakeStreamScope:
    """Scope.stream stand-in: records each call; stop_at = record index that is stopped before its data."""
    dev = types.SimpleNamespace(zero_info=None)

    def __init__(self, stop_at=None):
        self.calls, self.stop_at = [], stop_at

    def awg_from_spec(self, spec):
        pass

    @staticmethod
    def build_capture(cap):
        return cap.build() if not cap.meta.get('kept', True) else cap

    def stream(self, st, every=1.0, emit=None, stop_flag=None, build=True, live=True):
        k = len(self.calls)
        b = build() if callable(build) else build
        self.calls.append((st.duration, b))
        self.live = live
        if k == self.stop_at:
            raise DscopeError('STOPPED', 'stream stopped on request before any data arrived')
        emit({'i': 0, 't_signal_s': st.duration, 't_wall_s': 0.0, 'final': True})
        acq = {'duration_s': st.duration, 't_start_wall': 100.0 + 10 * (k + 1), 'complete': True,
               'samples': int(st.duration * st.rate)}

        def full(k=k, acq=acq):
            return types.SimpleNamespace(meta={'applied': {'record': k}, 'acq': acq, 'hints': [], 'warnings': []},
                                         summary=lambda: {'analog': {}, 'digital': {}}, analog={}, digital={})
        if not b:
            return types.SimpleNamespace(meta={'kept': False, 'acq': acq}, build=full)
        return full()


def test_long_stream_refreshes_record_after_record(capsys):
    args = cli.build_parser().parse_args(['stream', '-c', 'D0-D7', '--rate', '100M', '--duration', '10s', '--no-save'])
    st, total, rec, chained = cli.stream_plan(args)
    assert chained and rec == pytest.approx(4.0)
    fs = FakeStreamScope()
    cli.cmd_stream(args, fs)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [c[1] for c in fs.calls] == [False, False, True]       # only the last record is decoded and kept
    assert fs.live                                                 # sent while acquired unless --record-first
    assert sum(c[0] for c in fs.calls) == pytest.approx(10.0)
    res = lines[-1]
    assert res['records'] == 3 and res['signal_s'] == pytest.approx(10.0)
    assert res['seams_s'] == [pytest.approx(6.0), pytest.approx(6.0)]      # fake clock: 10 s apart, 4 s records
    assert [l['record'] for l in lines[:-1]] == [0, 1, 2] and lines[1]['t_signal_s'] == pytest.approx(8.0)


def test_refreshing_stream_stopped_between_records_keeps_the_previous_one(capsys):
    args = cli.build_parser().parse_args(['stream', '-c', 'D0-D7', '--rate', '100M', '--duration', 'forever',
                                          '--no-save'])
    fs = FakeStreamScope(stop_at=2)
    cli.cmd_stream(args, fs)
    res = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert res['stopped_by'] == 'stop_request' and res['records'] == 2 and res['applied'] == {'record': 1}


def test_stream_samples_keep_their_density_when_records_refresh():
    args = cli.build_parser().parse_args(['stream', '-c', 'A1', '--duration', '10s', '--samples', '500M'])
    st, total, rec, chained = cli.stream_plan(args)
    assert chained and st.rate == pytest.approx(50e6)              # not the 100 kSa/s default


def test_log_stopped_before_data_is_not_a_device_error(tmp_path):
    pl = make_plan(*A2D8, 1e6, None)

    class NoHead(FakeScope):
        def _run(self, prog, geom, *a, **kw):
            assert kw['stop_flag']()
            raw = RawCapture()
            raw.stopped = raw.aborted = True
            return raw
    res = logger.run(NoHead(b'', pl['packet_bytes']), settings(*A2D8), pl, None, stop_flag=lambda: True)
    assert res['ok'] and res['acq']['stop_reason'] == 'stop_request' and res['acq']['samples'] == 0


def test_short_stream_is_one_record():
    args = cli.build_parser().parse_args(['stream', '-c', 'A1', '--duration', '5s'])
    st, total, rec, chained = cli.stream_plan(args)
    assert not chained and total == rec == 5.0


def test_forever_stream_and_long_window_refresh():
    args = cli.build_parser().parse_args(['stream', '-c', 'A1', '--duration', '2h'])
    st, total, rec, chained = cli.stream_plan(args)
    assert chained and st.rate == cli.STREAM_MIN_RATE and rec == pytest.approx(4000.0)
    args = cli.build_parser().parse_args(['stream', '-c', 'A0-A3,D0-D7', '--rate', '20M', '--duration', 'forever'])
    st, total, rec, chained = cli.stream_plan(args)
    assert chained and total is None and rec * 20e6 * 5 <= 512 * 2 ** 20


def test_bit_stats_match_the_unpacked_count():
    rng = np.random.default_rng(1)
    bits = (rng.random(10_000) < 0.3).astype(np.uint8)
    bits[100:400] = 1
    p = np.packbits(bits, bitorder='little')
    ones, edges, last = packet.bit_stats(p)
    assert ones == bits.sum() and edges == np.count_nonzero(np.diff(bits.astype(np.int8))) and last == bits[-1]
    o1, e1, l1 = packet.bit_stats(p[:500])
    o2, e2, l2 = packet.bit_stats(p[500:], l1)          # the edge between the pieces is counted once
    assert (o1 + o2, e1 + e2, l2) == (ones, edges, last)


def test_packed_assembler_matches_the_unpacked_one():
    pl = make_plan(*A2D8, 1e6, 1)
    layout, an, dg, stream = fake_signal(pl, 0.01)
    head = b'\x55\x55\x55\x55' + bytes(4092)
    ev_u = packet.StreamAssembler(layout, continuous=True).feed(head + stream)
    ev_p = packet.StreamAssembler(layout, continuous=True, packed=True).feed(head + stream)
    u, p = ev_u[1]['digital'], ev_p[1]['digital']
    for ch in layout.digital:
        np.testing.assert_array_equal(np.unpackbits(p[ch], bitorder='little'), u[ch])
    np.testing.assert_array_equal(ev_p[1]['analog'][2], ev_u[1]['analog'][2])


def test_ram_ring_keeps_the_newest_data(tmp_path):
    rate = 1e6
    budget = 3 * 2 ** 20                                   # ~1 s of 2 analog + 8 digital
    pl = make_plan(*A2D8, rate, None, tmp_path, ram_budget=budget, every=0.25)
    assert pl['ram_ring']
    layout, an, dg, stream = fake_signal(pl, 8.0)
    n_tr = int(3.0 * pl['_data_bps'] / pl['packet_bytes'])  # stop after ~3 s of signal
    res = logger.run(FakeScope(stream, pl['packet_bytes'], max_transfers=n_tr), settings(*A2D8), pl,
                     str(tmp_path / 'ring'))
    f = res['files']
    cap = load_capture(f['npz'])
    n = len(cap.analog['A2'])
    assert f['t0_s'] > 1.5 and f['kept_s'] == pytest.approx(n / rate)
    assert budget - 3 * 64 * 3 < n * 3 <= budget                         # the budget is filled to the block
    k0 = int(round(f['t0_s'] * rate))
    np.testing.assert_array_equal(cap.analog['A2'], an[2][k0:k0 + n])              # the newest samples, in order
    np.testing.assert_array_equal(cap.digital['D3'], dg[3][k0:k0 + n])
    assert cap.meta['t0_analog_s'] == pytest.approx(f['t0_s']) and cap.meta['acq']['ram_ring']


def test_write_npz_matches_savez(tmp_path):
    parts = [np.arange(7, dtype=np.uint8), np.arange(3, dtype=np.uint8)]
    logger.write_npz(str(tmp_path / 'a.npz'), {'x': 1}, [('analog_A1', parts)])
    z = np.load(str(tmp_path / 'a.npz'))
    np.testing.assert_array_equal(z['analog_A1'], np.concatenate(parts))
    assert json.loads(bytes(z['meta']).decode()) == {'x': 1}


def test_bench_without_device_records_the_allowance(tmp_path, capsys):
    assert cli.main(['bench', '--no-device', '--ram', '1GB', '--disk', '20GB', '--dir', str(tmp_path / 'data'),
                     '--disk-test-mb', '8']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['allow']['ram_gb'] == 1.0 and out['bench']['decode_mb_s'] > 0 and out['bench']['disk_write_mb_s'] > 0
    h = host.load()
    assert h['allow']['disk_bytes'] == 20e9 and h['allow']['data_dir'].endswith('data')
    assert host.data_dir('x') == h['allow']['data_dir'] and not os.listdir(str(tmp_path / 'data'))
    h['bench']['usb_readout_mb_s'] = 206.0
    host.save(h)
    assert cli.main(['bench', '--no-device', '--ram', '2GB', '--disk-test-mb', '8']) == 0     # keeps USB + disk allowance
    h = host.load()
    assert h['bench']['usb_readout_mb_s'] == 206.0 and h['allow']['ram_bytes'] == 2e9 and h['allow']['disk_bytes'] == 20e9


def test_one_digital_channel_alone(tmp_path):
    """The stale back-fill must not write into a view of the read-only USB bytes (review finding)."""
    pl = make_plan([], ['D0'], 1e6, 0.5, tmp_path)
    layout, an, dg, stream = fake_signal(pl, 1.0)
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings([], ['D0']), pl, str(tmp_path / 'd0'))
    assert res['ok'], res.get('error')
    cap = load_capture(res['files']['npz'])
    np.testing.assert_array_equal(cap.digital['D0'][7:], dg[0][7:len(cap.digital['D0'])])


def test_long_every_does_not_size_the_queue(tmp_path, monkeypatch):
    """Lines are assembled from pieces of at most PIECE_BYTES, so a long --every cannot fill the writer queue
    with one huge item (review finding)."""
    monkeypatch.setattr(logger, 'PIECE_BYTES', 2 ** 20)
    sizes = []
    real_put = logger._Writer.put

    def put(self, bufs, n, info):
        sizes.append(n)
        return real_put(self, bufs, n, info)
    monkeypatch.setattr(logger._Writer, 'put', put)
    pl = make_plan(*A2D8, 1e6, 3.0, tmp_path, every=2.0)          # 6 MB per statistics line
    layout, an, dg, stream = fake_signal(pl, 4.0)
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'e'),
                     emit=lines.append)
    assert res['ok'] and max(sizes) <= 2 ** 20 + pl['packet_bytes']
    assert [l['t_signal_s'] for l in lines] == [pytest.approx(2.0, abs=1e-3), pytest.approx(3.0, abs=1e-3)]
    assert lines[-1]['final'] and sum(l['D2']['edges'] for l in lines) == res['digital']['D2']['edges']


def test_forever_disk_log_stops_at_the_allowance(tmp_path):
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk', disk_budget=6e6, chunk_s=1.0)
    layout, an, dg, stream = fake_signal(pl, 1.0)
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'a'))
    assert res['ok'] and res['acq']['stop_reason'] == 'disk_allowance' and 'allowance' in res['warnings'][-1]
    assert 6e6 <= res['acq']['data_mb'] * 1e6 < 6e6 + 2 * pl['packet_bytes']


def test_writer_error_still_closes_the_files(tmp_path, monkeypatch):
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk', chunk_s=0.5)
    layout, an, dg, stream = fake_signal(pl, 1.0)
    calls = [0]
    real = logger.packet.bit_stats

    def flaky(x, prev=None):
        calls[0] += 1
        if calls[0] > 40:
            raise MemoryError('simulated')
        return real(x, prev)
    monkeypatch.setattr(logger.packet, 'bit_stats', flaky)
    fs = FakeScope(stream, pl['packet_bytes'], max_transfers=2000)   # bounded: the writer error, not the queue limit
    res = logger.run(fs, settings(*A2D8), pl, str(tmp_path / 'w'))
    assert not res['ok'] and res['error']['code'] == 'WRITE_FAILED'
    idx = json.load(open(res['files']['index']))
    assert idx['status'] == 'error' and idx['stop_reason'] == 'writer_error'


def test_ctrl_c_is_not_a_device_error(tmp_path):
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk', chunk_s=1.0)
    layout, an, dg, stream = fake_signal(pl, 1.0)

    class Interrupted(FakeScope):
        def _run(self, prog, geom, *a, **kw):
            kw['on_chunk'](b'UUUU' + bytes(4092))
            kw['on_chunk'](self.stream[:geom['pkt_bytes']])
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        logger.run(Interrupted(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'k'))
    idx = json.load(open(str(tmp_path / 'k' / 'index.json')))
    assert idx['status'] == 'interrupted' and idx['chunks']


def test_popcount_fallback_without_numpy2(monkeypatch):
    bits = (np.arange(1000) % 7 < 3).astype(np.uint8)
    p = np.packbits(bits, bitorder='little')
    want = packet.bit_stats(p)
    monkeypatch.delattr(np, 'bitwise_count', raising=False)
    assert packet.bit_stats(p) == want


def test_disk_chunks_are_capped(tmp_path):
    pl = make_plan(*A2D8, 50e6, 60, tmp_path, storage='disk', chunk_s=3600.0)
    assert pl['chunk_mb'] <= 2 ** 30 / 1e6 + 1 and any('chunk limited' in w for w in pl['warnings'])


# ------------------------------------------------------------------------------------------- interleaved loop
def test_interleaved_rates_and_programs():
    assert logger.max_rate(1, 0) == 400e6 and logger.max_rate(2, 4) == 200e6 and logger.max_rate(1, 3) == 200e6
    assert logger.max_rate(3, 0) == 100e6 and logger.max_rate(0, 2) == 400e6 and logger.max_rate(0, 8) == 100e6
    pl = make_plan(['A2'], [], 200e6, 10)
    regs = dict(pl['_prog'])
    assert pl['interleaved'] and regs[0x00] == 6 and regs[0x28] == 0x0102 and regs[0x15] == 2   # x4, divider 2
    pl = make_plan([], ['D0', 'D7'], 400e6, 10)
    regs = dict(pl['_prog'])
    assert pl['verdict'] == 'sustained' and regs[0x20] == 0x0202 and regs[0x17] == 1
    plain = make_plan(['A2'], [], 100e6, 1)
    assert not plain['interleaved'] and dict(plain['_prog'])[0x28] == 0x0100      # DSWave's fold 0 up to 100 MSa/s
    with pytest.raises(DscopeError) as ei:
        make_plan(['A0', 'A1', 'A2'], [], 200e6, 1)
    assert 'at most 100 MSa/s' in ei.value.message
    with pytest.raises(DscopeError):
        make_plan(['A2'], ['D0', 'D1', 'D2'], 400e6, 0.1)
    assert make_plan(['A2'], [], 300e6, 0.5)['rate_hz'] == 200e6                   # snapped to a supported rate


def test_interleaved_log_round_trip(tmp_path):
    pl = make_plan(['A2'], ['D0'], 200e6, 0.01, tmp_path)
    assert pl['interleaved']
    layout, an, dg, stream = fake_signal(pl, 0.02)
    assert layout.afold == 4 and layout.dfold == 4 and layout.digital_stale == 4
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(['A2'], ['D0']), pl, str(tmp_path / 'f'))
    cap = load_capture(res['files']['npz'])
    n = len(cap.analog['A2'])
    assert n == 2_000_000 and cap.rate('A2') == 200e6
    np.testing.assert_array_equal(cap.analog['A2'], an[2][:n])
    np.testing.assert_array_equal(cap.digital['D0'][4:], dg[0][4:n])


def test_odd_divider_log_round_trip(tmp_path):
    pl = make_plan(['A2'], ['D0'], 100e6 / 7, 0.01, tmp_path)               # 14.2857 MSa/s
    assert pl['divider'] == 7
    layout, an, dg, stream = fake_signal(pl, 0.02)
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(['A2'], ['D0']), pl, str(tmp_path / 'odd'))
    cap = load_capture(res['files']['npz'])
    assert cap.rate('A2') == pytest.approx(100e6 / 7) and len(cap.analog['A2']) == res['acq']['samples']
    assert res['acq']['duration_s'] == pytest.approx(len(cap.analog['A2']) * 7 / 100e6, abs=1e-6)


# ------------------------------------------------------------------------------------------- readout and guard
def test_backlog_meter_tracks_clock_drift_over_hours():
    D = 150e6
    for ppm in (-150, 0, 150):                                    # scope clock vs PC clock
        m = logger.BacklogMeter(D, 0.0)
        t = 0.0
        while t < 3 * 3600:                                       # 3 h kept up: received = what the scope made
            t += 0.1
            assert m.update(t, D * (1 + ppm * 1e-6) * t - 1e6 * ((t * 10) % 3)) < 4e6     # +- a few packets
        got = D * (1 + ppm * 1e-6) * t
        for k in range(40):                                       # a 4 s host stall late in the run
            m.update(t + 0.1 * (k + 1), got)
        assert m.backlog == pytest.approx(D * 4.0 * (1 + ppm * 1e-6), abs=3e6)   # a fixed tolerance hid 160 MB here
    m = logger.BacklogMeter(300e6, 0.0)                           # readout-limited: grows at D - R
    for k in range(1, 51):
        m.update(0.1 * k, 200e6 * 0.1 * k)
    assert m.backlog == pytest.approx(100e6 * 5.0, rel=0.01) and m.growth == pytest.approx(100e6, rel=0.01)


def readout_clock(monkeypatch, pkt, readout):
    """The fake scope delivers one packet per pkt / readout seconds of the (patched) monotonic clock."""
    clock = Clock(pkt / readout)
    monkeypatch.setattr(logger.time, 'perf_counter', clock)
    return clock


def test_measure_readout_caches_this_channel_set(monkeypatch):
    pl = make_plan(*A2D8, 100e6, 10, defer=True)
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 200e6)
    layout, an, dg, stream = fake_signal(pl, 0.05)
    r, kept_up = logger.measure_readout(FakeScope(stream, pl['packet_bytes'], clock=clock), pl)
    assert r == pytest.approx(200e6, rel=0.01) and not kept_up
    c = host.readout_get(host.readout_key(2, 8, 100e6))
    assert c['mb_s'] == pytest.approx(200, rel=0.01) and not c['lower_bound'] and c['data_mb_s'] == 300.0
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 298e6)  # within 1 %: the host keeps up, R >= D
    assert logger.measure_readout(FakeScope(stream, pl['packet_bytes'], clock=clock), pl) == (300e6, True)
    assert host.readout_get(host.readout_key(2, 8, 100e6)) == dict(c, mb_s=300.0, lower_bound=True,
                                                                    time=host.readout_get(host.readout_key(2, 8, 100e6))['time'])
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 288e6)  # 4 % short: measured, not rounded up to D
    r, kept_up = logger.measure_readout(FakeScope(stream, pl['packet_bytes'], clock=clock), pl)
    assert r == pytest.approx(288e6, rel=0.01) and not kept_up


def test_forced_log_stops_at_ring_full_and_remembers_the_readout(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 150e6)
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, None, force=True, every=0.1)
    assert pl['gapfree_max_s'] == pytest.approx(150e6 * 200e6 / (300e6 * 100e6), abs=0.01)    # 1.0 s
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 210e6)  # a little better than remembered
    layout, an, dg, stream = fake_signal(pl, 0.05)
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes'], clock=clock), settings(*A2D8), pl, None, emit=lines.append)
    assert res['ok'] and res['acq']['stop_reason'] == 'ring_full' and 'gap-free' in res['warnings'][-1]
    assert res['acq']['duration_s'] == pytest.approx(150e6 * 210e6 / (300e6 * 90e6), rel=0.03)   # 1.17 s
    assert 145 <= res['acq']['unread_at_stop_mb'] <= 160
    proj = [l for l in lines if 'ring_full_in_s' in l]
    assert proj and proj[-1]['backlog_growth_mb_s'] == pytest.approx(90, rel=0.05)
    assert proj[-1]['gapfree_projected_s'] == pytest.approx(res['acq']['duration_s'], rel=0.05)
    c = host.readout_get(host.readout_key(2, 8, 100e6))
    assert c['mb_s'] == pytest.approx(210, rel=0.03) and not c['lower_bound']


def test_forced_ram_log_never_exceeds_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 20e6)              # gap-free 0.13 s: planned for ~50 MB
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, 60, tmp_path, force=True, ram_budget=int(60e6))
    assert pl['_ram_limit'] == 60e6
    layout, an, dg, stream = fake_signal(pl, 0.05)                 # the host keeps up after all
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 300e6)
    res = logger.run(FakeScope(stream, pl['packet_bytes'], clock=clock), settings(*A2D8), pl, str(tmp_path / 'r'))
    assert res['ok'] and res['acq']['stop_reason'] == 'ram_budget' and 60 <= res['acq']['data_mb'] < 62


class ProbeScope(FakeScope):
    dev = types.SimpleNamespace(zero_info=None)
    awg = None

    def _check_link(self):
        pass

    def awg_from_spec(self, spec):
        self.awg = spec


def test_cli_log_measures_an_unknown_readout_first(monkeypatch, capsys):
    args = cli.build_parser().parse_args(['log', '-c', 'A2,A3,D0-D7', '--rate', '100M', '--duration', '30s',
                                          '--no-save', '--awg', 'sine,1k,0V,1V'])
    st, pl = cli.log_plan(args, defer=True)
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 200e6)
    layout, an, dg, stream = fake_signal(pl, 0.05)
    fs = ProbeScope(stream, pl['packet_bytes'], clock=clock)
    with pytest.raises(DscopeError) as ei:                         # measured 200 MB/s: 3.4 s gap-free < 30 s
        cli.cmd_log(args, fs)
    assert ei.value.code == 'NEEDS_FORCE' and 'measured' in ei.value.plan['readout_source']
    assert fs.awg is None                                          # refused: the AWG was left alone
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert out[0]['probe']['readout_mb_s'] == pytest.approx(200, rel=0.01) and not out[0]['probe']['kept_up']
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 100e6)             # keeps the forced fake run short
    args.force = True                                              # remembered: no second probe
    fs = ProbeScope(stream, pl['packet_bytes'], clock=clock)
    assert cli.cmd_log(args, fs) == 0 and len(fs.calls) == 1 and fs.awg == 'sine,1k,0V,1V'
    res = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert res['ok'] and res['acq']['stop_reason'] == 'ring_full'


def test_stream_is_sent_while_acquired_unless_record_first():
    st = scopemod.Settings()
    st.analog, st.digital = list(A4D8[0]), list(A4D8[1])
    st.rate, st.duration, st.trig_mode = 50e6, 1.0, 'force'
    hal, scale, warnings, _ = scopemod.hal_config(st)
    hal['trigger']['enable'] = 0
    for live, mode in ((1, 2), (0, 0)):                            # 250 MB/s: above DSWave's 102,400 kB/s switch
        hal['stream_always'] = live
        prog, geom = mso_adapter.compile(hal, scopemod._cached_zero())
        assert dict(prog)[0x00] & 3 == mode and bool(geom['roll']) == bool(live)
    a = cli.build_parser().parse_args(['stream', '-c', 'D0', '--record-first'])
    assert a.record_first and not cli.build_parser().parse_args(['stream', '-c', 'D0']).record_first


def test_remeasure_replaces_a_cached_readout(monkeypatch, capsys):
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)    # stale: 30 s would be refused
    args = cli.build_parser().parse_args(['log', '-c', 'A2,A3,D0-D7', '--rate', '100M', '--duration', '0.5s',
                                          '--no-save', '--remeasure'])
    cli._budget(args)                                              # not refused before the device is opened
    st, pl = cli.log_plan(args, defer=True)
    assert pl['provisional'] and pl['_needs_probe']
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 280e6)
    layout, an, dg, stream = fake_signal(pl, 0.05)
    assert cli.cmd_log(args, ProbeScope(stream, pl['packet_bytes'], clock=clock)) == 0
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert out[0]['probe']['readout_mb_s'] == pytest.approx(280, rel=0.01) and out[-1]['ok']
    assert out[-1]['plan']['readout_mb_s'] == pytest.approx(280, rel=0.01)


def test_reported_limits_are_never_above_the_real_ones():
    pl = make_plan(*A2D8, 100e6, 1)                                # 2.8979 s at the assumed 190 MB/s
    assert pl['gapfree_max_s'] == 2.89 and pl['max_duration_s'] == 2.89
    assert make_plan(*A2D8, 100e6, pl['gapfree_max_s'])['recommendation'] == 'log'   # the number the plan gave


def test_stream_command_keeps_the_vertical_settings():
    st = settings(['A2'], ['D0'])
    st.probe, st.vdiv, st.offset, st.threshold = {'A2': 10.0}, {'A2': 2.0}, {'A2': 1.5}, 2.5
    host.readout_put(host.readout_key(1, 1, 400e6), 200.0, False, 450.0)
    with pytest.raises(DscopeError) as ei:
        logger.plan(st, 400e6, 60, zero_info=scopemod._cached_zero(), storage='none')
    cmd = ei.value.plan['stream_alternative']['command']
    args = cli.build_parser().parse_args(cmd.split()[3:])          # what the user would run
    got = scopemod.vertical_from_args(args)
    assert got.probe['A2'] == 10.0 and got.vdiv['A2'] == 2.0 and got.offset['A2'] == 1.5 and got.threshold == 2.5


def test_forced_disk_plan_counts_the_chunks_it_writes(tmp_path):
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, 3600, tmp_path, storage='disk', chunk_s=1.0, force=True)
    assert pl['chunks'] == math.ceil(1.25 * 503316480 * 200e6 / (300e6 * 100e6) / pl['chunk_s'])


def test_force_does_not_turn_an_unplanned_guard_stop_into_success(tmp_path, monkeypatch):
    pl = make_plan(*A2D8, 1e6, None, tmp_path, storage='disk', chunk_s=1.0, force=True)   # sustained: force unused
    assert not pl['_force']
    layout, an, dg, stream = fake_signal(pl, 1.0)
    clock = Clock(3 * pl['packet_bytes'] / pl['_data_bps'])         # the host falls behind (a stall)
    monkeypatch.setattr(logger.time, 'perf_counter', clock)
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 20e6)
    res = logger.run(FakeScope(stream, pl['packet_bytes'], clock=clock), settings(*A2D8), pl, str(tmp_path / 'f'))
    assert not res['ok'] and res['error']['code'] == 'HOST_TOO_SLOW' and res['acq']['stop_reason'] == 'backlog'


def test_guard_is_checked_on_every_transfer(monkeypatch):
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 20e6)
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, None, force=True)
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 200e6)
    layout, an, dg, stream = fake_signal(pl, 0.05)
    fs = FakeScope(stream, pl['packet_bytes'], clock=clock, pass_len=32)   # stop() only every 32 transfers
    res = logger.run(fs, settings(*A2D8), pl, None)
    assert res['ok'] and res['acq']['stop_reason'] == 'ring_full'
    # every transfer after the one that crossed the guard was dropped, not logged
    assert res['acq']['data_mb'] * 1e6 <= fs.delivered - 5 * pl['packet_bytes']
    accepted_at = res['acq']['data_mb'] * 1e6 / 200e6               # host time when the last kept transfer came
    assert 300e6 * accepted_at - res['acq']['data_mb'] * 1e6 <= 20e6 + 2 * pl['packet_bytes']


def test_a_scope_that_stops_sending_is_stalled_not_ring_full(monkeypatch):
    monkeypatch.setattr(logger, 'BACKLOG_STOP', 200e6)
    host.readout_put(host.readout_key(2, 8, 100e6), 200.0, False, 300.0)
    pl = make_plan(*A2D8, 100e6, None, force=True)
    clock = readout_clock(monkeypatch, pl['packet_bytes'], 300e6)

    class Dead(FakeScope):
        def _run(self, prog, geom, *a, **kw):
            raw = RawCapture()
            raw.head = b'\x55\x55\x55\x55' + bytes(4092)
            kw['on_chunk'](raw.head)
            for _ in range(20):
                clock.tick()
                kw['on_chunk'](self.stream[:self.pkt])
            while not kw['stop_flag']():                            # nothing more arrives
                clock.t += 0.01
            raw.stop = 'normal'
            return raw
    layout, an, dg, stream = fake_signal(pl, 0.05)
    res = logger.run(Dead(stream, pl['packet_bytes']), settings(*A2D8), pl, None)
    assert not res['ok'] and res['error']['code'] == 'TIMEOUT_DATA' and res['acq']['stop_reason'] == 'stalled'


def test_runs_remember_the_readout_only_when_it_is_clear():
    pl = {'_readout_key': host.readout_key(2, 8, 100e6)}
    D = 300e6

    def meter(received, t_end, step=0.05):
        m = logger.BacklogMeter(D, 0.0)
        t = 0.0
        while t < t_end - 1e-9:
            t += step
            m.update(t, received(t))
        return m
    m = meter(lambda t: 280e6 * t, 2.0)                             # readout-limited from the start
    assert logger._remember_readout(pl, m, 280e6, D) == (280.0, False)
    m = meter(lambda t: D * t, 60.0)                                # kept up
    assert logger._remember_readout(pl, m, D, D) == (300.0, True)

    def stall(t):                                                   # kept up, 0.3 s stall at 58 s, then 316 MB/s
        if t < 58.0:
            return D * t
        if t < 58.3:
            return D * 58.0
        return min(D * t, D * 58.0 + 316e6 * (t - 58.3))
    m = meter(stall, 60.0)                                          # still catching up at the stop
    assert m.backlog > logger.DRIFT_BAND
    assert logger._remember_readout(pl, m, stall(60.0) / 60.0, D) is None
    assert host.readout_get(pl['_readout_key'])['mb_s'] == 300.0    # the earlier value stays


def test_meter_does_not_absorb_a_slow_real_backlog():
    D, R = 200e6, 199.4e6                                           # 0.3 % short: 512 MiB after ~895 s
    m = logger.BacklogMeter(D, 0.0)
    t = 0.0
    while D * t - R * t < 500 * 2 ** 20:
        t += 0.1
        m.update(t, R * t)
    assert m.backlog > logger.BACKLOG_STOP                          # the guard fires before the ring overwrites


def test_stream_live_flag_reaches_the_register_program(monkeypatch, capsys):
    sc = scopemod.Scope()
    seen = []

    class Stop(Exception):
        pass

    def compile_ok(hal, warnings):
        seen.append(hal['stream_always'])
        raise Stop()
    monkeypatch.setattr(sc, '_check_link', lambda: None)
    monkeypatch.setattr(sc, '_compile_ok', compile_ok)
    for live in (True, False):
        st = settings(['A2'], [])
        st.rate, st.duration = 100e6, 0.1
        with pytest.raises(Stop):
            sc.stream(st, live=live)
    assert seen == [1, 0]
    for argv, live in ((['stream', '-c', 'D0-D7', '--rate', '100M', '--duration', '1s', '--no-save'], True),
                       (['stream', '-c', 'D0-D7', '--rate', '100M', '--duration', '1s', '--no-save', '--record-first'],
                        False),
                       (['stream', '-c', 'D0-D7', '--rate', '100M', '--duration', '10s', '--no-save', '--record-first'],
                        False)):
        fs = FakeStreamScope()
        cli.cmd_stream(cli.build_parser().parse_args(argv), fs)
        assert fs.live is live
    capsys.readouterr()
