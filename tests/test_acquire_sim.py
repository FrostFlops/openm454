"""Acquisition engine against a simulated M454 (no hardware).

The simulator enforces the rules recovered from DSWave (docs/PROTOCOL.md section 7):
  * while the device runs, control transfers are only legal between EP 0x84 bytes 02 and 03
  * the device streams until it is stopped; it must never find EP 0x86 without a pending read
  * after a failed handshake the host must not send anything
"""
import threading
import time

import pytest
import usb1

from openm454 import acquire as acq
from openm454 import regs
from openm454.acquire import Engine, HEAD_BYTES
from openm454.errors import DscopeError
from openm454.transport import Transport, REQ_WRITE, REQ_READ_SETUP, REQ_READ, usbfs_memory_mb

PKT = 8192


class Sim:
    def __init__(self, head_after=0.0, trigger=True, ep84='ok', zlp_at=None, fail_hs_at=None, submit_error=None):
        self.regs = {}
        self.log = []              # ('W'|'R'|'EP84'|'cancel', ...)
        self.violations = []
        self.pending = []          # submitted transfers, in order
        self.running = False       # CTRL=1 written, not yet stopped by the handshake
        self.streaming = False
        self.hs = 0                # 0 idle, 1 after byte 01, 2 inside (after byte 02)
        self.hs_count = 0
        self.head_after = head_after
        self.trigger = trigger
        self.ep84_mode = ep84
        self.fail_hs_at = fail_hs_at
        self.zlp_at = zlp_at
        self.submit_error = submit_error   # raised by submit(): a host-side failure, nothing reaches the device
        self.t_run = None
        self.head_sent = False
        self.data_sent = 0
        self.starved = False
        self.after_fail = []       # control transfers seen after a handshake failure
        self.failed = False

    # ---------------------------------------------------------------- device behaviour
    def ctrl(self, kind, addr, val=None):
        if self.failed:
            self.after_fail.append((kind, addr, val))
        if self.running and self.hs != 2:
            self.violations.append('%s 0x%02X outside handshake' % (kind, addr))
        self.log.append((kind, addr, val, self.hs == 2))
        if kind == 'W':
            self.regs[addr] = val
            if addr == regs.CTRL and val == 1:
                self.running = self.streaming = True
                self.t_run = time.time()
            if addr == regs.ACTION and val == 8 and self.hs == 2:
                self.streaming = False

    def ep84(self):
        self.hs_count += 1
        if self.ep84_mode == 'timeout' or (self.fail_hs_at is not None and self.hs_count >= self.fail_hs_at):
            self.failed = True
            raise usb1.USBErrorTimeout()
        self.log.append(('EP84', self.hs))
        if self.hs == 0:
            self.hs = 1
            return 1
        if self.hs == 1:
            self.hs = 2
            return 2
        self.hs = 0
        if not self.streaming:
            self.running = False
        return 3

    def pump(self):
        done = []
        for tr in list(self.pending):
            if tr._cancel:
                tr._finish(usb1.TRANSFER_CANCELLED, 0)
                done.append(tr)
        for tr in done:
            self.pending.remove(tr)
            tr._fire()
        if not self.streaming:
            return
        if not self.head_sent:
            if not self.trigger or time.time() - self.t_run < self.head_after:
                return
            head = [tr for tr in self.pending if tr._n == HEAD_BYTES]
            if not head:
                self.starved = True
                return
            tr = head[0]
            tr._buf[:4] = b'\x55\x55\x55\x55'
            tr._finish(usb1.TRANSFER_COMPLETED, HEAD_BYTES)
            self.pending.remove(tr)
            self.head_sent = True
            tr._fire()
            return
        data = [tr for tr in self.pending if tr._n != HEAD_BYTES]
        if not data:
            self.starved = True
            return
        for tr in data[:4]:
            self.data_sent += 1
            n = 0 if self.zlp_at is not None and self.data_sent >= self.zlp_at else tr._n
            tr._buf[:n] = bytes([self.data_sent & 0xFF]) * n
            tr._finish(usb1.TRANSFER_COMPLETED, n)
            self.pending.remove(tr)
            tr._fire()


class FakeTransfer:
    def __init__(self, sim):
        self.sim = sim
        self._submitted = False
        self._cancel = False

    def setBulk(self, ep, n, callback=None, timeout=0):
        assert ep == 0x86 and timeout == 0
        self._n, self._cb, self._buf = n, callback, bytearray(n)

    def submit(self):
        assert not self._submitted
        if self.sim.submit_error is not None:    # as libusb: the transfer is refused, never becomes pending
            raise self.sim.submit_error
        self._submitted, self._cancel = True, False
        self.sim.pending.append(self)

    def isSubmitted(self):
        return self._submitted

    def cancel(self):
        self.sim.log.append(('cancel',))
        self._cancel = True

    def _finish(self, status, n):
        self._status, self._len = status, n

    def _fire(self):
        self._submitted = False
        self._cb(self)

    def getStatus(self):
        return self._status

    def getActualLength(self):
        return self._len

    def getBuffer(self):
        return self._buf

    def close(self):
        assert not self._submitted


class FakeHandle:
    def __init__(self, sim):
        self.sim = sim
        self._read = None

    def getTransfer(self):
        return FakeTransfer(self.sim)

    def controlWrite(self, rt, req, v, i, data, timeout=0):
        self.sim.pump()
        if req == REQ_WRITE:
            self.sim.ctrl('W', data[0], data[1] | data[2] << 8)
        elif req == REQ_READ_SETUP:
            self._read = data[0]
        return len(data)

    def controlRead(self, rt, req, v, i, n, timeout=0):
        assert req == REQ_READ
        self.sim.pump()
        self.sim.ctrl('R', self._read)
        return (0x0006).to_bytes(2, 'little')[:n]

    def bulkRead(self, ep, n, timeout=0):
        assert ep == 0x84 and n == 1
        self.sim.pump()
        return bytes([self.sim.ep84()])


class FakeCtx:
    def __init__(self, sim):
        self.sim = sim

    def handleEventsTimeout(self, tv=0):
        self.sim.pump()
        time.sleep(0.001)


class FakeDevice:
    def __init__(self, sim):
        self.t = Transport()
        self.t.h, self.t.ctx = FakeHandle(sim), FakeCtx(sim)


SETUP = [(regs.CTRL, 0), (0x10, 0x1234), (regs.MODE, 0), (regs.FLUSH_DATA, 0)]


def engine(**kw):
    sim = Sim(**kw)
    return sim, Engine(FakeDevice(sim))


def writes(sim):
    return [(e[1], e[2]) for e in sim.log if e[0] == 'W']


def test_triggered_capture_keeps_reads_queued_until_stop():
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=5 * PKT, n_transfers=4, trigger_timeout=1.0)
    assert cap.stop == 'normal' and cap.head is not None and len(cap.data) >= 5 * PKT
    assert sim.violations == [] and not sim.starved
    assert cap.ep84 == [1, 2, 3]                        # one handshake: the stop
    w = writes(sim)
    assert w[-4:] == [(regs.CTRL, 0), (regs.ACTION, 8), (regs.CTRL, 0), (regs.ACTION, 4)]
    # the CTRL=0/ACTION=8 pair is inside the handshake, the final pair after every read was cancelled and back
    inside = [e for e in sim.log if e[0] == 'W' and e[3]]
    assert [(e[1], e[2]) for e in inside] == [(regs.CTRL, 0), (regs.ACTION, 8)]
    last_cancel = max(i for i, e in enumerate(sim.log) if e[0] == 'cancel')
    assert sim.log[-2][:3] == ('W', regs.CTRL, 0) and last_cancel < len(sim.log) - 2
    assert not any(tr.isSubmitted() for tr in eng._trs)
    assert not eng.t.running and not eng.t.failed


def test_start_order_and_setup():
    sim, eng = engine()
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    w = writes(sim)
    assert w[:4] == SETUP and w[4:7] == [(regs.SYNC, 0), (regs.ACTION, 0), (regs.CTRL, 1)]


def test_no_trigger_polls_progress_then_stops_normally():
    sim, eng = engine(trigger=False)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=0.4)
    assert cap.head is None and cap.timed_out and cap.stop == 'normal'
    assert len(cap.progress) >= 5 and set(cap.progress) == {6}
    assert sim.violations == []
    kinds = [h[1] for h in cap.handshakes]
    assert kinds[-1] == 'stop' and set(kinds[:-1]) == {'poll'}
    assert cap.handshakes[0][0] >= 100                  # polling starts 100 ms after the start writes
    assert cap.ep84 == [1, 2, 3] * len(cap.handshakes)
    reads = [e for e in sim.log if e[0] == 'R']
    assert reads and all(e[1] == regs.ACTION and e[3] for e in reads)


def test_failed_stop_handshake_sends_nothing_more():
    sim, eng = engine(ep84='timeout')
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=2 * PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR'
    assert sim.after_fail == []                         # no control transfer after the failed EP 0x84 read
    assert eng.t.failed and not eng.t.running
    assert not any(tr.isSubmitted() for tr in eng._trs)
    with pytest.raises(DscopeError) as ei:
        eng.t.write(regs.CTRL, 0)
    assert ei.value.code == 'DEVICE_ERROR'
    with pytest.raises(DscopeError):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT)


def test_failed_poll_handshake_goes_to_error_stop():
    sim, eng = engine(trigger=False, fail_hs_at=4)      # 01 02 03 then the next poll times out
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR' and 'EP 0x84' in ei.value.message
    assert sim.after_fail == [] and sim.violations == []


def test_zero_length_data_is_an_error_stop():
    sim, eng = engine(zlp_at=3)
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=100 * PKT, n_transfers=4, trigger_timeout=1.0)
    assert 'zero-length' in ei.value.message
    assert eng.t.failed and sim.violations == []
    assert writes(sim)[-1] == (regs.CTRL, 1)            # nothing written after the start


def test_register_access_refused_while_running():
    sim, eng = engine()
    seen = []

    def on_chunk(buf):
        if len(buf) == PKT and not seen:
            with pytest.raises(DscopeError) as ei:
                eng.t.write(regs.CTRL, 0)
            seen.append(ei.value.code)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=3 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=on_chunk)
    assert seen == ['UNSAFE_OPERATION'] and cap.stop == 'normal' and sim.violations == []


def test_abort_from_another_thread_stops_normally():
    sim, eng = engine(trigger=False)
    threading.Timer(0.25, eng.abort).start()
    t0 = time.time()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=10.0)
    assert time.time() - t0 < 2.0
    assert cap.aborted and cap.stop == 'normal' and sim.violations == []
    assert eng.wait_idle(0.1)


def test_exception_in_on_chunk_still_stops_cleanly():
    sim, eng = engine()

    def boom(buf):
        if len(buf) == PKT:
            raise ValueError('decoder bug')
    with pytest.raises(ValueError):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=4 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=boom)
    assert sim.violations == [] and not eng.t.running and not eng.t.failed
    assert writes(sim)[-2:] == [(regs.CTRL, 0), (regs.ACTION, 4)]


def test_repeat_frames_mode_does_not_poll():
    sim, eng = engine(head_after=0.3)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=None, n_transfers=2, trigger_timeout=1.0, repeat_frames=3)
    assert len(cap.frames) == 3 and cap.progress == [] and cap.stop == 'normal'


def test_runs_are_spaced_100ms():
    sim, eng = engine()
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    t_end = eng._t_end
    sim.head_sent, sim.streaming = False, False
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert sim.t_run - t_end >= 0.1


def test_device_close_sends_nothing_after_failure():
    from openm454.device import Device
    sim = Sim()
    d = Device()
    d.t.h, d.t.ctx = FakeHandle(sim), FakeCtx(sim)
    d.opened = True
    d.t.fail('test')
    n = len(sim.log)
    d.t.close = lambda: None
    d.close()
    assert len(sim.log) == n


# ---------------------------------------------------------------------------------------------- review fixes
import _thread
import signal


def test_ctrl_c_while_waiting_for_trigger_stops_normally():
    sim, eng = engine(trigger=False)
    before = signal.getsignal(signal.SIGINT)
    threading.Timer(0.25, _thread.interrupt_main).start()
    with pytest.raises(KeyboardInterrupt):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=10.0)
    assert sim.violations == [] and not sim.running and not sim.failed
    assert writes(sim)[-4:] == [(regs.CTRL, 0), (regs.ACTION, 8), (regs.CTRL, 0), (regs.ACTION, 4)]
    assert not eng.t.failed and not eng.t.running
    assert signal.getsignal(signal.SIGINT) is before


def test_ctrl_c_while_collecting_stops_normally():
    sim, eng = engine()

    def slow(buf):
        time.sleep(0.003)
    threading.Timer(0.2, _thread.interrupt_main).start()
    with pytest.raises(KeyboardInterrupt):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=10_000 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=slow)
    assert sim.violations == [] and not sim.starved and not sim.running
    assert writes(sim)[-2:] == [(regs.CTRL, 0), (regs.ACTION, 4)]


def test_error_after_record_complete_gets_handshake_stop_but_no_final_writes():
    sim, eng = engine(zlp_at=3)            # transfers 3.. are zero-length, in the batch that completes the record
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=4, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR' and 'zero-length' in ei.value.message
    inside = [(e[1], e[2]) for e in sim.log if e[0] == 'W' and e[3]]
    assert inside == [(regs.CTRL, 0), (regs.ACTION, 8)]         # DSWave: normal stop first ...
    assert writes(sim)[-1] == (regs.ACTION, 8)                  # ... then the error: no ForceAcquire writes
    assert not sim.running and sim.violations == [] and eng.t.failed
    assert not any(tr.isSubmitted() for tr in eng._trs)


class FailWriteHandle(FakeHandle):
    def __init__(self, sim, fail):
        super().__init__(sim)
        self.fail = fail

    def controlWrite(self, rt, req, v, i, data, timeout=0):
        if req == REQ_WRITE and (data[0], data[1] | data[2] << 8) == self.fail and self.sim.hs == 2:
            self.sim.log.append(('Wfail', data[0]))
            raise usb1.USBErrorTimeout()
        return super().controlWrite(rt, req, v, i, data, timeout)


def test_failed_write_inside_stop_handshake_completes_the_handshake():
    sim = Sim()
    dev = FakeDevice(sim)
    dev.t.h = FailWriteHandle(sim, (regs.CTRL, 0))
    eng = Engine(dev)
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=2 * PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR'
    tail = [e for e in sim.log if e[0] in ('W', 'Wfail', 'EP84')][-5:]
    # 01, 02, failed W CTRL=0, W ACTION=8 still sent, 03 still read (RegQueueThread 0x34b8c0)
    assert [e[0] for e in tail] == ['EP84', 'EP84', 'Wfail', 'W', 'EP84']
    assert tail[3][1:3] == (regs.ACTION, 8)
    assert not sim.running and sim.hs == 0 and eng.t.failed
    assert writes(sim)[-1] == (regs.ACTION, 8)                  # no final writes after the failure


class RaisingTransfer(FakeTransfer):
    calls = 0

    def getBuffer(self):
        RaisingTransfer.calls += 1
        if self._n != HEAD_BYTES and RaisingTransfer.calls == 2:
            raise MemoryError('simulated')
        return self._buf


def test_exception_in_callback_is_an_error_stop_not_a_silent_loss():
    sim = Sim()
    dev = FakeDevice(sim)
    RaisingTransfer.calls = 0
    dev.t.h.getTransfer = lambda: RaisingTransfer(sim)
    eng = Engine(dev)
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=50 * PKT, n_transfers=4, trigger_timeout=1.0)
    assert 'exception in the transfer callback' in ei.value.message
    assert writes(sim)[-1] == (regs.CTRL, 1) and sim.violations == []
    assert not any(tr.isSubmitted() for tr in eng._trs)


def test_first_run_waits_100ms_after_the_last_register_write():
    sim, eng = engine()
    eng._t_end = 0.0
    eng.t.write(0x10, 5)                    # e.g. the open's idle pair or an AWG write
    t0 = time.time()
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert sim.t_run - t0 >= 0.100 + 0.050  # gap + submit-to-start


def test_shutdown_is_sticky():
    sim, eng = engine()
    eng.shutdown()
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT)
    assert ei.value.code == 'TIMEOUT_WATCHDOG' and sim.log == []


def test_close_abandons_handle_with_stale_transfers():
    closed = []

    class H:
        def releaseInterface(self, i):
            closed.append('release')

        def close(self):
            closed.append('close')
    t = Transport()
    t.h, t.ctx, t.stale = H(), None, True
    t.close()
    assert closed == [] and t.h is None


class OpenHandle:
    """Answers the open sequence: firmware/panel/bin versions, HW_STATUS, PASS already set, EEPROM reads."""
    def __init__(self, done=True):
        self.ops, self._read, self.done = [], None, done

    def controlWrite(self, rt, req, v, i, data, timeout=0):
        if req == REQ_WRITE:
            self.ops.append(('W', data[0], data[1] | data[2] << 8, time.time()))
        else:
            self._read = data[0]
            self.ops.append(('R', data[0], None, time.time()))
        return len(data)

    def controlRead(self, rt, req, v, i, n, timeout=0):
        val = {0xF0: 0x3110, 0xF2: 0x3100, 0xF1: 0x3100, 0xFF: 0x0082 if self.done else 0x0002,
               regs.CTRL: 0x0004}.get(self._read, 0x1111)
        return (val.to_bytes(2, 'little') * 64)[:n]


def test_open_stops_a_left_over_run_before_anything_else():
    from openm454.device import Device
    d = Device()
    h = OpenHandle(done=True)

    def fake_open():
        d.t.h, d.t.speed = h, 4
        return d.t
    d.t.open = fake_open
    d.open()
    ops = h.ops
    i_ff = next(i for i, o in enumerate(ops) if o[0] == 'R' and o[1] == 0xFF)
    assert [o[:3] for o in ops[:i_ff + 1] if o[0] == 'W'] == []          # only reads before the status
    assert [o[:3] for o in ops[i_ff + 1:i_ff + 3]] == [('W', regs.CTRL, 0), ('W', regs.ACTION, 4)]
    assert ops[i_ff + 3][3] - ops[i_ff + 2][3] >= 0.095                 # 100 ms after the reset
    assert [o[:3] for o in ops[-2:]] == [('W', regs.CTRL, 0), ('W', regs.ACTION, 8)]
    n = len(ops)
    d.close()
    assert len(h.ops) == n                                               # close sends nothing


def test_lock_excludes_a_second_holder(tmp_path, monkeypatch):
    from openm454 import lock
    p = tmp_path / 'openm454.lock'
    p.write_text('1234567890123')                  # longer than any pid: the old code locked the wrong byte
    monkeypatch.setattr(lock, 'LOCK_PATH', str(p))
    monkeypatch.setattr(lock, 'dswave_pids', lambda: [])
    a = lock.DeviceLock().acquire()
    with pytest.raises(DscopeError) as ei:
        lock.DeviceLock().acquire()
    assert ei.value.code == 'BUSY'
    a.release()
    b = lock.DeviceLock().acquire()
    b.release()


# ---------------------------------------------------------------------------------------------- second review
def test_caller_error_in_on_chunk_gets_a_normal_stop_and_is_reraised():
    sim, eng = engine()

    def emit_like(buf):
        if len(buf) == PKT:
            raise DscopeError('UNSAFE_OPERATION', 'e.g. an AWG write from a stream callback')
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=4 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=emit_like)
    assert ei.value.code == 'UNSAFE_OPERATION'                   # not turned into a device failure
    assert writes(sim)[-4:] == [(regs.CTRL, 0), (regs.ACTION, 8), (regs.CTRL, 0), (regs.ACTION, 4)]
    assert not sim.running and not eng.t.failed and sim.violations == []


def test_caller_error_in_stop_flag_gets_a_normal_stop():
    sim, eng = engine(trigger=False)

    def flag():
        raise DscopeError('DECODE_ERROR', 'caller bug')
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0, stop_flag=flag)
    assert ei.value.code == 'DECODE_ERROR' and not sim.running and not eng.t.failed


def test_stop_flag_marks_a_user_stop():
    sim, eng = engine(trigger=False)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=5.0, stop_flag=lambda: True)
    assert cap.stopped and cap.aborted and cap.stop == 'normal'


def test_bad_head_with_abort_pending_gets_the_handshake_stop():
    class BadHeadSim(Sim):
        def pump(self):
            if self.streaming and not self.head_sent and eng_ref[0]._abort.is_set() is False:
                eng_ref[0].abort()               # abort is pending when the malformed head arrives
            super().pump()
    sim = BadHeadSim()
    dev = FakeDevice(sim)
    eng = Engine(dev)
    eng_ref = [eng]
    orig = FakeTransfer._finish

    def bad(self, status, n):
        if n == HEAD_BYTES:
            self._buf[:4] = b'\x00\x00\x00\x00'
        orig(self, status, n)
    FakeTransfer._finish = bad
    try:
        with pytest.raises(DscopeError) as ei:
            eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    finally:
        FakeTransfer._finish = orig
    assert 'bad head' in ei.value.message
    inside = [(e[1], e[2]) for e in sim.log if e[0] == 'W' and e[3]]
    assert inside == [(regs.CTRL, 0), (regs.ACTION, 8)] and not sim.running


def test_second_run_on_a_busy_engine_is_refused():
    sim, eng = engine()
    seen = []

    def reenter(buf):
        if not seen:
            with pytest.raises(DscopeError) as ei:
                eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT)
            seen.append(ei.value.code)
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=2 * PKT, n_transfers=2, trigger_timeout=1.0, on_chunk=reenter)
    assert seen == ['BUSY'] and sim.violations == [] and not sim.running


def test_abandon_detaches_libusb_finalizers():
    detached = []

    class Fin:
        def __init__(self, name):
            self.name = name

        def detach(self):
            detached.append(self.name)
    t = Transport()
    t.h = type('H', (), {})()
    t.h.close = Fin('handle')
    t.ctx = type('C', (), {})()
    t.ctx._USBContext__close = Fin('context')
    tr = type('T', (), {})()
    tr._USBTransfer__close = Fin('transfer')
    t.stale, t.stale_transfers = True, [tr]
    t.close()
    assert sorted(detached) == ['context', 'handle', 'transfer'] and t.h is None and t.ctx is None


def test_cold_open_configures_the_fpga_first(monkeypatch):
    from openm454 import device as devmod
    import openm454.firmware
    monkeypatch.setattr(openm454.firmware, 'bitstream', lambda: b'\x00' * 1000)
    d = devmod.Device()
    h = OpenHandle(done=False)

    def bulk_write(ep, data, timeout=0):
        h.ops.append(('B', ep, len(data), time.time()))
        h.done = True
        return len(data)
    h.bulkWrite = bulk_write

    def fake_open():
        d.t.h, d.t.speed = h, 4
        return d.t
    d.t.open = fake_open
    d.open()
    w = [o[:3] for o in h.ops if o[0] in 'WB']
    assert w[0] == ('W', regs.PROG_B, 0xFFFE)                     # FPGA configuration is the first write
    assert ('W', regs.ACTION, 4) not in w                         # no reset pair on a cold device
    assert [o[:3] for o in h.ops[-2:]] == [('W', regs.CTRL, 0), ('W', regs.ACTION, 8)]
    assert d.info['fpga_configured_now']


def test_register_access_after_a_stop_waits_like_dswave():
    """DSWave sleeps 100 ms after every stop. On hardware, writes sent within ~10 ms of the stop that ends a run
    without a trigger were acknowledged but dropped (an AWG program was lost)."""
    sim, eng = engine(trigger=False)
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=0.2)
    t_stop = time.time()
    eng.t.write(0x08, 3)                                # e.g. the first AWG write right after the capture
    assert time.time() - t_stop >= 0.08
    assert writes(sim)[-3:] == [(regs.CTRL, 0), (regs.ACTION, 4), (0x08, 3)]


def _open_with(monkeypatch, done, **kw):
    from openm454 import device as devmod
    import openm454.firmware
    monkeypatch.setattr(openm454.firmware, 'bitstream', lambda: b'\x00' * 1000)
    d = devmod.Device()
    h = OpenHandle(done=done)

    def bulk_write(ep, data, timeout=0):
        h.ops.append(('B', ep, len(data), time.time()))
        h.done = True
        return len(data)
    h.bulkWrite = bulk_write

    def fake_open():
        d.t.h, d.t.speed = h, 4
        return d.t
    d.t.open = fake_open
    d.open(**kw)
    return d, [o[:3] for o in h.ops if o[0] in 'WB']


def test_recover_reloads_a_configured_fpga_as_the_first_write(monkeypatch):
    d, w = _open_with(monkeypatch, done=True, reload_fpga=True)
    assert w[:2] == [('W', regs.PROG_B, 0xFFFE), ('W', regs.PROG_B, 0x0001)]
    assert ('B', 0x02, 1000) in w and ('W', regs.ACTION, 4) not in w
    assert d.info['fpga_configured_now'] and d.info['fpga_was_done']


def test_warm_open_never_touches_prog_b(monkeypatch):
    d, w = _open_with(monkeypatch, done=True)
    assert not any(o[1] in (regs.PROG_B, regs.BULK_LO, regs.BULK_HI) for o in w if o[0] == 'W')
    assert not any(o[0] == 'B' for o in w) and not d.info['fpga_configured_now']


def test_silent_mcu_gets_the_recover_then_replug_hint():
    from openm454.device import Device
    d = Device()

    class Dead(OpenHandle):
        def controlRead(self, *a, **k):
            raise usb1.USBErrorTimeout()

    def fake_open():
        d.t.h, d.t.speed = Dead(), 4
        return d.t
    d.t.open = fake_open
    d.t.close = lambda: None
    with pytest.raises(DscopeError) as ei:
        d.open()
    assert ei.value.code == 'USB_ERROR' and 'recover' in ei.value.hint and 'replug' in ei.value.hint


def test_endless_run_without_buffering_stops_on_the_flag():
    """Loop mode: no record end, no collect deadline, data only goes to on_chunk."""
    sim, eng = engine()
    got = []
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=None, n_transfers=4, trigger_timeout=1.0, collect_timeout=None,
                  on_chunk=got.append, stop_flag=lambda: len(got) > 50, poll=False, keep_data=False)
    assert cap.stop == 'normal' and cap.stopped and not cap.timed_out
    assert len(cap.data) == 0 and cap.n_data == sum(len(b) for b in got[1:]) >= 50 * PKT
    assert sim.violations == [] and not sim.starved and cap.ep84 == [1, 2, 3]


def test_usbfs_limit_is_read_from_sysfs(tmp_path):
    p = tmp_path / 'usbfs_memory_mb'
    p.write_text('1000\n', encoding='ascii')
    assert usbfs_memory_mb(str(p)) == 1000
    p.write_text('0\n', encoding='ascii')                       # devio.c: "0 = no limit"
    assert usbfs_memory_mb(str(p)) == 0
    assert usbfs_memory_mb(str(tmp_path / 'missing')) is None    # Windows, macOS, no sysfs
    p.write_text('nonsense\n', encoding='ascii')
    assert usbfs_memory_mb(str(p)) is None


def test_a_zero_limit_means_unlimited(monkeypatch):
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 0)
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=128 * 1024, need_bytes=128 * 1024, n_transfers=16, trigger_timeout=1.0)
    assert cap.stop == 'normal' and sim.violations == []


def test_a_capture_larger_than_the_usbfs_buffer_is_refused_before_the_setup_program(monkeypatch):
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 1)       # 1 MiB: not even 4 reads of 256 KiB fit
    sim, eng = engine()
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=256 * 1024, need_bytes=256 * 1024, n_transfers=16, trigger_timeout=1.0)
    assert ei.value.code == 'USB_NO_MEM' and ei.value.exit_code == 6
    assert '4198400 B' in ei.value.message and '1 MiB' in ei.value.message    # 16 x 256 KiB + the 4 KiB head
    assert 'usbfs_memory_mb' in ei.value.hint
    assert sim.log == [] and sim.pending == []                   # refused before any write: the device is untouched
    assert not eng.t.running and not eng.t.failed


def test_a_small_usbfs_budget_lowers_the_queue_depth(monkeypatch):
    """A budget that cannot hold the geometry's queue uses what fits (>= 4) instead of failing the run."""
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 8)       # 8 MiB: the head + 7 reads of 1 MiB fit
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=1024 * 1024, need_bytes=4 * 1024 * 1024, n_transfers=16, trigger_timeout=1.0)
    assert cap.transfers == 7 and len(eng._trs) == 8            # head + 7 data reads
    assert cap.stop == 'normal' and sim.violations == []


def test_a_budget_that_holds_the_queue_keeps_it(monkeypatch):
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 1000)
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=16, trigger_timeout=1.0)
    assert cap.transfers == 16 and len(eng._trs) == 17


def test_the_streaming_path_uses_the_budget_too(monkeypatch):
    """log/stream (keep_data=False, no record end): the same cap, and the run still ends on the stop flag."""
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 8)
    sim, eng = engine()
    got = []
    cap = eng.run(SETUP, pkt_bytes=1024 * 1024, need_bytes=None, n_transfers=16, trigger_timeout=1.0,
                  collect_timeout=None, on_chunk=got.append, stop_flag=lambda: len(got) > 12, poll=False,
                  keep_data=False)
    assert cap.transfers == 7 and cap.stop == 'normal' and cap.stopped and len(cap.data) == 0
    assert sim.violations == [] and sim.pending == []


def test_no_usbfs_keeps_the_requested_depth(monkeypatch):
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: None)   # Windows, macOS
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=16, trigger_timeout=1.0)
    assert cap.transfers == 16


def test_submit_no_mem_names_the_usbfs_limit(monkeypatch):
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: 16)
    sim, eng = engine(submit_error=usb1.USBError(usb1.ERROR_NO_MEM))
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'USB_NO_MEM' and 'usbfs buffer is 16 MiB' in ei.value.message
    assert 'usbfs_memory_mb' in ei.value.hint
    assert sim.pending == [] and not eng.t.running               # nothing queued, nothing started


def test_no_mem_without_usbfs_stays_a_plain_usb_error(monkeypatch):
    """Windows, macOS: no usbfs buffer, so NO_MEM is plain host memory and the reopen hint still applies."""
    monkeypatch.setattr(acq, 'usbfs_memory_mb', lambda: None)
    sim, eng = engine(submit_error=usb1.USBError(usb1.ERROR_NO_MEM))
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'USB_ERROR' and 'NO_MEM' in ei.value.message
    assert 'replug' in ei.value.hint and sim.pending == []
