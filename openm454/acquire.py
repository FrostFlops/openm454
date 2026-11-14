"""Acquisition engine: run one register-programmed capture and collect the raw stream.

Mirrors DSWave 1.2.1 (UsbDevice_TransferThread 0x33f950, UsbBsp3x Setup/Start/StopAcquire and the UsbTransfer
callbacks; RVAs refer to DSWave.exe 1.2.1):

  1. >= 100 ms after the previous run and after the last register write: setup program
     (W 0x01=0, settle, MSO writes ..., W 0x00 MODE, W 0xF9=0)
  2. submit EP 0x86 reads: one 4096-byte head transfer + N data transfers (no timeout); wait 50 ms
  3. start: W SYNC=sync, W ACTION=0, W CTRL=1; settle.  From the moment W CTRL=1 is sent until the stop handshake is
     done every register access sits inside an EP 0x84 handshake (Transport.running / handshake / handshake_writes)
  4. collect: data transfers are resubmitted from their callback for the WHOLE run, also after the record is
     complete, because the device keeps streaming zero padding until it is stopped.  While the head has not
     arrived the progress register is polled like DSWave (handshake + R 0x02, every ~15 ms from 100 ms on)
  5. normal stop: handshake{W CTRL=0, W ACTION=8}; stop resubmitting; cancel every transfer and wait until all
     are back; W CTRL=0, 1 ms, W ACTION=4
     error stop (failed handshake or control transfer, bad head, transfer error, zero-length transfer,
     resubmit failure): nothing more is sent to the device; cancel, wait, mark the session failed and raise
     DEVICE_ERROR.  As in DSWave's transfer thread, a completed record or an abort is handled before an error that
     arrived with it: such an error gets the normal stop handshake and drain, but not the final writes.

Linux charges every submitted EP 0x86 read against the kernel usbfs buffer (usbfs_memory_mb, 16 MiB by default, 0
= no limit) in full, so a capture whose reads do not all fit is refused before the setup program (USB_NO_MEM, the
message carries the bytes and the limit) or fails at submit with libusb NO_MEM when other processes hold part of the
buffer. DSWave runs on Windows, where the USB stack has no such limit.

Ctrl+C (SIGINT) during a run only requests the normal stop; KeyboardInterrupt is raised after the device has
been stopped. Exceptions inside the libusb callback become an error stop instead of being lost. Exceptions from the
caller's on_chunk / stop_flag are not device failures: the run is stopped normally and the exception re-raised.

DSWave's "1 ms" sleeps last ~15 ms on the Windows timer tick (recorded 14-31 ms), so the settle times below use
15 ms to stay inside the recorded envelope.
"""
import collections
import signal
import struct
import threading
import time

import usb1

from . import regs
from .errors import DscopeError
from .transport import HINT_REOPEN, HINT_USBFS, usbfs_memory_mb

HEAD_BYTES = 4096
HEAD_MAGIC = b'\x55\x55\x55\x55'
EP_DATA = 0x86
GAP_BETWEEN_RUNS = 0.100       # FUN_14033f270: 100 ms before every run
SETUP_SETTLE = 0.015           # SetupAcquire: "1 ms" after W CTRL=0 (recorded 14-31 ms)
SUBMIT_TO_START = 0.050        # TransferThread: 50 ms between submitting and the start writes
START_SETTLE = 0.015           # StartAcquire: "1 ms" after W CTRL=1 (first EP 0x84 read >= 14.7 ms later)
FINAL_GAP = 0.001              # ForceAcquire: W CTRL=0, 1 ms, W ACTION=4 (recorded 1.6-29 ms)
POLL_AFTER = 0.100             # progress polling starts 100 ms after the start writes
POLL_EVERY = 0.015
DRAIN_TIMEOUT = 2.0            # cancelled transfers must be back within this time
MAX_EVENTS_PER_PASS = 32       # events handled between two checks of abort / stop_flag / deadlines

_STATUS = {usb1.TRANSFER_COMPLETED: 'COMPLETED', usb1.TRANSFER_ERROR: 'ERROR', usb1.TRANSFER_TIMED_OUT: 'TIMED_OUT',
           usb1.TRANSFER_CANCELLED: 'CANCELLED', usb1.TRANSFER_STALL: 'STALL', usb1.TRANSFER_NO_DEVICE: 'NO_DEVICE',
           usb1.TRANSFER_OVERFLOW: 'OVERFLOW'}


def _queued_bytes(pkt_bytes, n_transfers):
    """What a run submits at once: the head read plus every data read (the kernel charges each of them in full)."""
    return HEAD_BYTES + n_transfers * pkt_bytes


def _usbfs_error(what, pkt_bytes, n_transfers, limit):
    """USB_NO_MEM naming the numbers the kernel checks: the bytes this run queues and the usbfs limit in MiB."""
    return DscopeError('USB_NO_MEM', '%s: %d B of EP 0x86 reads queued at once (%d x %d B + a %d B head), the Linux '
                       'usbfs buffer is %d MiB' % (what, _queued_bytes(pkt_bytes, n_transfers), n_transfers,
                                                   pkt_bytes, HEAD_BYTES, limit), HINT_USBFS)


class RawCapture:
    def __init__(self):
        self.head = None
        self.data = bytearray()
        self.n_data = 0           # data bytes received (also counted when keep_data=False leaves data empty)
        self.frames = []          # repeat mode: one entry per data transfer (frame data + 4 KiB tail)
        self.t_start = None
        self.t_head = None
        self.t_done = None
        self.timed_out = False
        self.aborted = False      # ended early: abort() / shutdown() / SIGINT / stop_flag
        self.stopped = False      # ... by the caller's stop_flag (a user stop request)
        self.ep84 = []            # every EP 0x84 handshake byte, in order (None = timeout)
        self.handshakes = []      # (ms after start, 'poll'|'stop', [bytes])
        self.progress = []        # raw R 0x02 values from the progress polls
        self.stop = None          # 'normal' | 'error' | None (never started)
        self.error = None

    @property
    def head_words(self):
        return struct.unpack('<8I', self.head[:32]) if self.head else None

    @property
    def triggered(self):
        w = self.head_words
        return None if w is None else not (w[2] & 0x80000000)


class Engine:
    def __init__(self, device):
        self.dev = device
        self.t = device.t
        self.sync = 0
        self.active = False
        self._t_end = time.time()
        self._abort = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._state = threading.Lock()
        self._shutdown = False
        self._stale = []          # transfers that never came back after a cancel (kept alive, never freed)
        self._sigint = False

    # ------------------------------------------------------------------ cross-thread control
    def abort(self):
        """Ask a running capture to stop (thread-safe). The run loop performs the normal stop handshake."""
        self._abort.set()

    def shutdown(self):
        """Sticky abort (watchdog): stop the current run and refuse every later one."""
        with self._state:
            self._shutdown = True
        self._abort.set()

    def wait_idle(self, timeout):
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------ transfers
    def _submit(self, n):
        tr = self.t.h.getTransfer()
        tr.setBulk(EP_DATA, n, callback=self._cb, timeout=0)
        self._trs.append(tr)
        tr.submit()
        return tr

    def _cb(self, tr):
        """Runs inside libusb event handling (handleEventsTimeout and every synchronous transfer, e.g. the EP 0x84
        handshake reads). Data transfers are resubmitted here, like UsbTransfer_DataCallback, so the reads stay
        queued during handshakes. An exception must not escape: ctypes would swallow it and the transfer would be
        lost silently, so it becomes an error event."""
        try:
            if self._stopping or self.t.failed:
                return                  # DataCallback: stopping or device in error -> free, no resubmit
            st = tr.getStatus()
            n = tr.getActualLength()
            if tr is self._head_tr:
                if st != usb1.TRANSFER_COMPLETED:
                    self._events.append(('error', 'head transfer ended with status %s' % _STATUS.get(st, st)))
                    return
                buf = bytes(memoryview(tr.getBuffer())[:n])
                if len(buf) != HEAD_BYTES or buf[:4] != HEAD_MAGIC:
                    self._events.append(('error', 'bad head packet (%d bytes, check id %s)' % (len(buf), buf[:4].hex())))
                else:
                    self._events.append(('head', buf))
                return
            if st != usb1.TRANSFER_COMPLETED:
                self._events.append(('error', 'data transfer ended with status %s' % _STATUS.get(st, st)))
                return
            if n == 0:
                self._events.append(('error', 'zero-length data transfer'))
                return
            if not self._full:
                self._events.append(('data', bytes(memoryview(tr.getBuffer())[:n])))
            try:
                tr.submit()
            except usb1.USBError as e:
                self._events.append(('error', 'data transfer resubmit failed: %s' % e))
        except BaseException as e:      # noqa: BLE001 - see docstring
            self._events.append(('error', 'exception in the transfer callback: %r' % (e,)))

    def _cancel_and_wait(self):
        for tr in self._trs:
            try:
                if tr.isSubmitted():
                    tr.cancel()
            except usb1.USBError:
                pass
        end = time.time() + DRAIN_TIMEOUT
        while any(tr.isSubmitted() for tr in self._trs):
            if time.time() > end:
                stale = [tr for tr in self._trs if tr.isSubmitted()]
                self._stale.extend(stale)
                self.t.stale = True
                self.t.stale_transfers.extend(stale)
                return False
            self.t.ctx.handleEventsTimeout(0.02)
        self._events.clear()
        for tr in self._trs:
            try:
                tr.close()
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------ run
    def run(self, setup_writes, pkt_bytes, need_bytes, n_transfers=16, trigger_timeout=5.0,
            collect_timeout=10.0, repeat_frames=0, on_chunk=None, stop_flag=None, poll=True, settle=0.0,
            keep_data=True):
        """Execute one capture. need_bytes = data bytes (after the head) to collect.
        repeat_frames > 0: repeat mode; after the head every data transfer is one frame (data + 4 KiB tail);
        collect that many frames. on_chunk(bytes) is called for the head and for every data chunk as it arrives
        (live streaming). stop_flag(): optional callable; the capture stops when it returns True.
        settle: extra seconds between the setup writes and the start (front end recovering from overdrive).
        collect_timeout=None: no deadline after the head (loop mode streams until stop_flag ends it).
        keep_data=False: data chunks only go to on_chunk (cap.data stays empty, cap.n_data counts them), so an
        endless stream does not grow in memory.
        Returns a RawCapture; cap.head is None when nothing arrived before trigger_timeout. Raises DEVICE_ERROR
        after an error stop (the session is then unusable until the device is reopened)."""
        t = self.t
        with self._state:
            if self._shutdown:
                raise DscopeError('TIMEOUT_WATCHDOG', 'the session is shutting down (watchdog)')
            if self.active:
                raise DscopeError('BUSY', 'a capture is already running on this engine')
            self._abort.clear()
            self._idle.clear()
            self.active = True
        self._sigint = False
        restore = None
        try:
            if threading.current_thread() is threading.main_thread():
                def on_sigint(signum, frame):
                    self._sigint = True
                    self._abort.set()
                try:
                    restore = (signal.signal(signal.SIGINT, on_sigint),)
                except (ValueError, OSError):
                    restore = None
            if t.failed:
                raise DscopeError('DEVICE_ERROR', 'device failed earlier in this session (%s)' % t.failed, HINT_REOPEN)
            gap = max(self._t_end, t.t_last_write) + GAP_BETWEEN_RUNS - time.time()
            if gap > 0:
                time.sleep(gap)
            cap = self._run(setup_writes, pkt_bytes, need_bytes, n_transfers, trigger_timeout, collect_timeout,
                            repeat_frames, on_chunk, stop_flag, poll, settle, keep_data)
        finally:
            self._t_end = time.time()
            self.active = False
            self._idle.set()
            if restore is not None:              # last: a SIGINT after this may raise right here
                try:
                    signal.signal(signal.SIGINT, restore[0] if restore[0] is not None else signal.default_int_handler)
                except (ValueError, OSError):
                    pass
        if self._sigint:
            raise KeyboardInterrupt
        return cap

    def _run(self, setup_writes, pkt_bytes, need_bytes, n_transfers, trigger_timeout, collect_timeout,
             repeat_frames, on_chunk, stop_flag, poll, settle=0.0, keep_data=True):
        t = self.t
        limit = usbfs_memory_mb()          # Linux only (0 = no limit): checked before the setup program
        # a lower bound: the kernel also charges a few hundred bytes per URB, and other processes share the limit
        if limit and _queued_bytes(pkt_bytes, n_transfers) > limit * 1024 * 1024:
            raise _usbfs_error('refusing this capture before the setup program', pkt_bytes, n_transfers, limit)
        cap = RawCapture()
        self._events = collections.deque()
        self._stopping = False
        self._full = False
        self._late_error = None
        self._user_exc = None               # exception from on_chunk / stop_flag (caller code)
        self._trs = []
        self._head_tr = None

        # the device is idle here: setup writes are direct control transfers
        for i, (addr, val) in enumerate(setup_writes):
            t.write(addr, val)
            if i == 0 and addr == regs.CTRL:
                time.sleep(SETUP_SETTLE)
        if settle > 0:
            self._abort.wait(settle)        # idle device: nothing is queued yet
        if self._abort.is_set():
            cap.aborted = True              # stopped before the start: nothing to stop
            return cap
        try:
            self._head_tr = self._submit(HEAD_BYTES)
            for _ in range(n_transfers):
                self._submit(pkt_bytes)
        except usb1.USBError as e:
            self._stopping = True           # nothing was started: just take the reads back
            self._cancel_and_wait()
            limit = usbfs_memory_mb() if getattr(e, 'value', None) == usb1.ERROR_NO_MEM else None
            if limit:                       # NO_MEM on a host with a usbfs limit: the buffer, not a device problem
                raise _usbfs_error('libusb NO_MEM while queueing', pkt_bytes, n_transfers, limit)
            raise DscopeError('USB_ERROR', 'cannot queue EP 0x86 reads: %s' % e, HINT_REOPEN)
        time.sleep(SUBMIT_TO_START)

        error = None
        try:
            try:
                with t.guarded('run'):
                    t.write(regs.SYNC, self.sync)
                    t.write(regs.ACTION, 0x0000)
                    t.write(regs.CTRL, 0x0001)      # Transport.running is set as this is sent
                time.sleep(START_SETTLE)
                cap.t_start = time.time()
                error = self._collect(cap, need_bytes, trigger_timeout, collect_timeout, repeat_frames, on_chunk,
                                      stop_flag, poll, keep_data)
            except DscopeError as e:            # failed start write or progress-poll handshake
                error = 'start write failed: %s' % e.message if not cap.t_start else e.message
            except BaseException as exc:        # a bug in the engine loop: stop like a user stop, then re-raise
                err = self._finish(cap, None)
                if err:
                    raise DscopeError('DEVICE_ERROR', err, HINT_REOPEN) from exc
                raise
            error = self._finish(cap, error)
        finally:
            if not self._stopping:              # something escaped before the reads were taken back
                t.fail(t.failed or 'interrupted while stopping')
                self._stopping = True
                t.running = False
                self._cancel_and_wait()
        cap.t_done = time.time()
        if error:
            raise DscopeError('DEVICE_ERROR', error, HINT_REOPEN) from self._user_exc
        if self._user_exc is not None:          # caller code failed: the device was stopped normally first
            raise self._user_exc
        return cap

    def _finish(self, cap, error):
        """Normal stop when possible, error stop otherwise. Returns the error that remains (None = clean)."""
        if error is None and self.t.running and not self.t.failed:
            return self._stop_normal(cap)
        self._stop_error(cap, error or self.t.failed or 'run ended in an unknown state')
        return cap.error

    def _collect(self, cap, need_bytes, trigger_timeout, collect_timeout, repeat_frames, on_chunk, stop_flag,
                 poll, keep_data=True):
        """Pump events until the record is complete, the caller stops, or a deadline passes. Returns None, or an
        error reason that requires the error stop."""
        t = self.t
        ctx = t.ctx
        deadline_head = cap.t_start + trigger_timeout
        deadline = None
        last_poll = 0.0
        polling = poll and not repeat_frames
        while True:
            ctx.handleEventsTimeout(0.005)
            budget = MAX_EVENTS_PER_PASS        # bounded, so stop requests and deadlines are always checked
            while self._events and budget > 0:
                budget -= 1
                kind, buf = self._events.popleft()
                if kind == 'error':
                    if self._full or self._abort.is_set():
                        # TransferThread checks 'frame over' / abort before error: stop normally first
                        self._late_error = self._late_error or buf
                        continue
                    return buf
                if kind == 'head':
                    cap.head = buf
                    cap.t_head = time.time()
                    deadline = cap.t_head + collect_timeout if collect_timeout is not None else None
                elif self._full:
                    continue                # zero padding after the record: discarded
                elif repeat_frames:
                    cap.frames.append(buf)
                else:
                    cap.n_data += len(buf)
                    if keep_data:
                        cap.data += buf
                if on_chunk is not None:
                    try:
                        on_chunk(buf)
                    except BaseException as e:  # caller code: normal stop, re-raised by _run
                        self._user_exc = e
                        return None
                if self._enough(cap, need_bytes, repeat_frames):
                    self._full = True
                ctx.handleEventsTimeout(0)  # keep resubmitting while a backlog is processed
            if self._full:
                return None
            if self._abort.is_set():
                cap.aborted = True
                return None
            if stop_flag is not None:
                try:
                    stop = stop_flag()
                except BaseException as e:      # caller code: normal stop, re-raised by _run
                    self._user_exc = e
                    return None
                if stop:
                    cap.aborted = cap.stopped = True
                    return None
            now = time.time()
            if cap.head is None:
                if now > deadline_head:
                    cap.timed_out = True
                    return None
                if polling and now - cap.t_start >= POLL_AFTER and now - last_poll >= POLL_EVERY:
                    last_poll = now
                    rec = []
                    try:
                        with t.handshake(rec):
                            v = t.read16(regs.ACTION)
                    finally:
                        cap.ep84 += rec
                        cap.handshakes.append((round((now - cap.t_start) * 1000, 1), 'poll', rec))
                    cap.progress.append(v)
            elif deadline is not None and now > deadline:
                cap.timed_out = True
                return None

    @staticmethod
    def _enough(cap, need_bytes, repeat_frames):
        if repeat_frames:
            return len(cap.frames) >= repeat_frames
        return cap.head is not None and need_bytes is not None and cap.n_data >= need_bytes

    # ------------------------------------------------------------------ stop
    def _stop_normal(self, cap):
        """DSWave StopAcquire + Drain + ForceAcquire. Returns None, or the reason the run ended in the error state."""
        t = self.t
        rec = []
        t_rel = round((time.time() - cap.t_start) * 1000, 1) if cap.t_start else None
        try:
            t.handshake_writes([(regs.CTRL, 0x0000), (regs.ACTION, 0x0008)], rec)
        except DscopeError as e:
            cap.ep84 += rec
            cap.handshakes.append((t_rel, 'stop', rec))
            self._stop_error(cap, 'stop handshake failed: %s' % e.message)
            return cap.error
        cap.ep84 += rec
        cap.handshakes.append((t_rel, 'stop', rec))
        if self._late_error is None:            # data-path errors that arrived during the handshake
            self._late_error = next((b for k, b in self._events if k == 'error'), None)
        self._stopping = True
        t.running = False
        if not self._cancel_and_wait():
            why = 'EP 0x86 reads did not come back within %.0f s of the cancel' % DRAIN_TIMEOUT
            t.fail(why)
            cap.stop, cap.error = 'error', why
            return why
        if self._late_error:                    # DSWave: error stop after the normal one -> ForceAcquire refused
            t.fail(self._late_error)
            cap.stop, cap.error = 'error', self._late_error
            return cap.error
        try:
            t.write(regs.CTRL, 0x0000)
            time.sleep(FINAL_GAP)
            t.write(regs.ACTION, 0x0004)
            t.hold(GAP_BETWEEN_RUNS)            # writes right after this can be dropped (run without trigger)
        except DscopeError as e:
            why = 'final stop write failed: %s' % e.message
            t.fail(why)
            cap.stop, cap.error = 'error', why
            return why
        cap.stop = 'normal'
        return None

    def _stop_error(self, cap, why):
        """DSWave error stop: send nothing; take the reads back; the session stays failed."""
        t = self.t
        t.fail(why)
        self._stopping = True
        t.running = False
        self._cancel_and_wait()
        cap.stop, cap.error = 'error', why
