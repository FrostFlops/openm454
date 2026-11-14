"""USB transport for the DSCope M454 with a hard safety whitelist.

Wire protocol (recovered from DSWave 1.2.1, hal::UsbBsp3x):
  register write : ctrl OUT bmRequestType 0x40, bRequest 0xB0, data [addr, lo, hi]
  register read  : ctrl OUT 0x40/0xB1 data [addr, len, ext]; wait 10 ms; ctrl IN 0xC0/0xB2 len bytes
  EP 0x02 bulk OUT : FPGA bitstream (and NVM writes, which this driver never does)
  EP 0x84 bulk IN  : 1-byte handshake used while an acquisition is running
  EP 0x86 bulk IN  : capture data (4096-byte head packet, then data packets)

Safety (never relax these):
  * only bRequest 0xB0/0xB1/0xB2 are ever sent (0xB3 = firmware ISP is impossible here)
  * writes to MCU registers 0xF0-0xFF are refused except FLUSH_DATA, HARDWARE_INDEX(0), and the
    FPGA-config / EEPROM-read registers inside their guarded contexts
  * EP 0x02 OUT is only reachable from fpga_config() and only while the FPGA reports not-configured

Acquisition rules (DSWave UsbBsp3x):
  * while an acquisition runs (`running`: from the W CTRL=1 start write until the stop handshake is done) every
    register access must sit inside an EP 0x84 handshake (handshake()); plain write()/read() are refused
  * after any failed handshake or failed control transfer while running the session is `failed` (DSWave
    "not ready"): nothing at all is sent to the device any more until it is reopened
"""
import os
import contextlib
import threading
import time

import usb1

from .errors import DscopeError
from . import regs

VID, PID = 0x2A0E, 0x0080
REQ_WRITE, REQ_READ_SETUP, REQ_READ = 0xB0, 0xB1, 0xB2
_ALLOWED_REQUESTS = (REQ_WRITE, REQ_READ_SETUP, REQ_READ)
EP_OUT, EP_CTRL_IN, EP_DATA_IN = 0x02, 0x84, 0x86
READ_DELAY = 0.015             # RegRead asks for 10 ms, which lasts 13-31 ms on the Windows timer tick (trace)
TIMEOUT_MS = 1000
HS_TIMEOUT_MS = 1000             # UsbBsp3x_RegBulkIn timeout
HINT_REOPEN = ('run the command once more: the next open first stops a run left behind (best effort). '
               'If that fails too (USB_ERROR or DEVICE_ERROR), unplug and replug the scope')
USBFS_MEMORY = '/sys/module/usbcore/parameters/usbfs_memory_mb'    # Linux usbfs buffer limit, MiB (default 16)
HINT_USBFS = ('Linux charges every queued USB read against the kernel usbfs buffer (usbfs_memory_mb, 16 MiB by '
              'default, shared by every USB device). Raise it for this boot with sudo sh -c "echo 1000 > '
              '/sys/module/usbcore/parameters/usbfs_memory_mb", or permanently with usbcore.usbfs_memory_mb=1000 '
              'on the kernel command line')
_ABANDONED = []                  # libusb objects that must never be closed or finalized (see Transport.close)


def usbfs_memory_mb(path=USBFS_MEMORY):
    """The usbfs buffer limit in MiB (0 = no limit), or None where there is no usbfs (Windows, macOS, no sysfs). Every
    submitted URB is charged against it in drivers/usb/core/devio.c (usbfs_increase_memory_usage): its length, plus
    sizeof(async) + sizeof(urb) (~300 B), plus one scatterlist entry per 16 KiB when the read needs more than one (about
    2.3 KiB on a 1 MiB read on xHCI); a control transfer in flight is charged too. Reads that do not all fit fail in
    libusb with NO_MEM (-11) at submit time; acquire._charge budgets the queue the same way."""
    try:
        with open(path, encoding='ascii') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Transport:
    def __init__(self):
        self.ctx = None
        self.dev = None
        self.h = None
        self.speed = None
        self._ctx_guard = set()          # active guarded contexts: 'fpga', 'nvm', 'security'
        self._fb_written = False         # D2: FPGA config must precede any EEPROM address write
        self._wrote_anything = False
        self.lock = threading.RLock()
        self.log = None                  # optional callable(str) for a wire log
        self.running = False             # CTRL=1 sent and the stop handshake not yet done
        self.failed = None               # reason once the device failed (no further I/O until reopened)
        self.stale = False               # EP 0x86 reads that never came back: libusb close would hang
        self.stale_transfers = []        # those transfers (their finalizers are detached when abandoned)
        self.t_last_write = 0.0          # time of the last register write (run spacing)
        self.quiet_until = 0.0           # no register access before this time (set after a stop, see hold())
        self._in_hs = False              # inside an EP 0x84 handshake

    # ---------------------------------------------------------------- open / close
    def open(self):
        if os.environ.get('OPENM454_NO_USB'):          # set by tests / CI: never touch a real scope
            raise DscopeError('NOT_FOUND', 'USB access is disabled (OPENM454_NO_USB is set)')
        self.ctx = usb1.USBContext()
        self.ctx.open()
        try:
            dev = self.ctx.getByVendorIDAndProductID(VID, PID, skip_on_error=True)
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'USB enumeration failed: %s' % e)
        if dev is None:
            raise DscopeError('NOT_FOUND', 'DSCope M454 (2A0E:0080) not found',
                              'check the USB cable; the device must show "USB-based DSL Instrument v3"')
        self.dev = dev
        self.speed = dev.getDeviceSpeed()
        try:
            self.h = dev.open()
        except usb1.USBError as e:
            raise DscopeError('BUSY', 'cannot open the device (%s)' % e,
                              'close DSWave or any other program using the scope, then retry')
        try:
            manu, prod = self.h.getManufacturer(), self.h.getProduct()
        except usb1.USBError:
            manu, prod = None, None
        if manu != 'DreamSourceLab' or prod != 'USB-based DSL Instrument v3':
            self.close()
            raise DscopeError('PROTOCOL_ERROR', 'unexpected USB strings: %r / %r' % (manu, prod))
        try:
            self.h.claimInterface(0)
        except usb1.USBError as e:
            self.h.close()
            self.h = None
            raise DscopeError('BUSY', 'cannot claim the USB interface (%s)' % e,
                              'close DSWave or a running "openm454 watch", then retry')
        return self

    def close(self):
        """Release the interface and the libusb handle. If transfers are still in flight (stale) or another thread
        holds the lock (a control transfer in progress), the libusb objects are abandoned instead: libusb1's close
        would wait for those transfers forever. The OS releases the handle when the process exits."""
        if self.h is None and self.ctx is None:
            return
        if self.stale or not self.lock.acquire(timeout=2.0):
            self._abandon()
            return
        try:
            if self.h is not None:
                with contextlib.suppress(Exception):
                    self.h.releaseInterface(0)
                with contextlib.suppress(Exception):
                    self.h.close()
                self.h = None
            if self.ctx is not None:
                with contextlib.suppress(Exception):
                    self.ctx.close()
                self.ctx = None
        finally:
            self.lock.release()

    def _abandon(self):
        """Keep the libusb objects alive and detach python-libusb1's weakref finalizers (handle, context and stale
        transfers), which would otherwise run at interpreter exit and loop forever on transfers that never
        complete. The OS releases the device when the process ends."""
        objs = [(self.h, 'close'), (self.ctx, '_USBContext__close')]
        objs += [(tr, '_USBTransfer__close') for tr in self.stale_transfers]
        for obj, attr in objs:
            fin = getattr(obj, attr, None) if obj is not None else None
            detach = getattr(fin, 'detach', None)
            if detach is not None:
                with contextlib.suppress(Exception):
                    detach()
        _ABANDONED.append((self.h, self.ctx, list(self.stale_transfers)))
        self.h = self.ctx = None

    # ---------------------------------------------------------------- raw control
    def _ctrl_out(self, req, data):
        assert req in _ALLOWED_REQUESTS, 'forbidden bRequest 0x%02X' % req
        try:
            n = self.h.controlWrite(0x40, req, 0, 0, bytes(data), timeout=TIMEOUT_MS)
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'control write 0x%02X failed: %s' % (req, e))
        if n != len(data):
            raise DscopeError('USB_ERROR', 'short control write (%d of %d)' % (n, len(data)))

    def _ctrl_in(self, req, n):
        assert req in _ALLOWED_REQUESTS, 'forbidden bRequest 0x%02X' % req
        try:
            return bytes(self.h.controlRead(0xC0, req, 0, 0, n, timeout=TIMEOUT_MS))
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'control read failed: %s' % e)

    # ---------------------------------------------------------------- registers
    def _check_write(self, addr, value):
        if not 0 <= addr <= 0xFF or not 0 <= value <= 0xFFFF:
            raise DscopeError('UNSAFE_OPERATION', 'register write out of range %r=%r' % (addr, value))
        if addr in (regs.SEC_CTRL, regs.SEC_DATA) and 'security' not in self._ctx_guard:
            raise DscopeError('UNSAFE_OPERATION', 'security registers only inside the handshake')
        if addr == regs.CTRL and value != 0 and 'run' not in self._ctx_guard:
            raise DscopeError('UNSAFE_OPERATION', 'only the acquisition engine may set CTRL (it must queue the '
                              'EP 0x86 reads first and stop the run with a handshake)')
        if addr < 0x80:
            return
        if addr == regs.FLUSH_DATA:
            return
        if addr == regs.HARDWARE_INDEX and value == 0:
            return
        if addr == regs.EEPROM_ADDR and 'nvm' in self._ctx_guard:
            return
        if addr in (regs.PROG_B, regs.BULK_LO, regs.BULK_HI) and 'fpga' in self._ctx_guard:
            return
        raise DscopeError('UNSAFE_OPERATION', 'refused write to MCU register 0x%02X (%s)' % (addr, regs.name(addr)))

    def _check_state(self, what):
        if self.failed:
            raise DscopeError('DEVICE_ERROR', '%s refused: the device failed earlier in this session (%s)'
                              % (what, self.failed), HINT_REOPEN)
        if self.running and not self._in_hs:
            raise DscopeError('UNSAFE_OPERATION', '%s while an acquisition is running (only allowed inside an '
                              'EP 0x84 handshake)' % what)

    def fail(self, reason):
        """Enter the error state (DSWave SetReady(0)): from now on no control transfer and no EP 0x84 read."""
        if not self.failed:
            self.failed = reason
            if self.log:
                self.log('DEVICE ERROR: %s' % reason)

    def hold(self, seconds):
        """Keep the register interface quiet for `seconds` (DSWave sleeps 100 ms after every stop before anything
        else, FUN_14033f270). Measured 2026-10-31: writes sent within ~10 ms of the stop that ends a run without a
        trigger are acknowledged on USB but silently dropped by the device."""
        self.quiet_until = max(self.quiet_until, time.time() + seconds)

    def _wait_quiet(self):
        wait = self.quiet_until - time.time()
        if wait > 0 and not self._in_hs:
            time.sleep(wait)

    def write(self, addr, value):
        self._wait_quiet()
        with self.lock:
            self._check_state('register write 0x%02X' % addr)
            self._check_write(addr, value)
            if addr == regs.EEPROM_ADDR:
                self._fb_written = True
            self._wrote_anything = True
            if addr == regs.CTRL and value & 1:
                self.running = True          # from the moment it is sent the device may be running
            self._ctrl_out(REQ_WRITE, [addr, value & 0xFF, (value >> 8) & 0xFF])
            self.t_last_write = time.time()
            if self.log:
                self.log('W %02X(%s)=%s' % (addr, regs.name(addr), '****' if addr == regs.SEC_DATA else '%04X' % value))

    def read(self, addr, n=2, ext=0):
        if not 1 <= n < 128:
            raise DscopeError('INVALID_ARG', 'register read length must be 1..127')
        self._wait_quiet()
        with self.lock:
            self._check_state('register read 0x%02X' % addr)
            self._ctrl_out(REQ_READ_SETUP, [addr, n, ext])
            time.sleep(READ_DELAY)
            d = self._ctrl_in(REQ_READ, n)
            if self.log:
                secret = addr in (regs.SEC_DATA, regs.EEPROM_ADDR)
                self.log('R %02X(%s) ext=%d -> %s' % (addr, regs.name(addr), ext, '<%d bytes>' % n if secret else d.hex()))
            return d

    def read16(self, addr, ext=0):
        return int.from_bytes(self.read(addr, 2, ext), 'little')

    # ---------------------------------------------------------------- guarded contexts
    @contextlib.contextmanager
    def guarded(self, name):
        self._ctx_guard.add(name)
        try:
            yield
        finally:
            self._ctx_guard.discard(name)

    def nvm_read(self, addr, n):
        """Read n bytes (n < 128) of the device EEPROM. Never writes the EEPROM."""
        with self.lock, self.guarded('nvm'):
            self.write(regs.EEPROM_ADDR, addr)
            return self.read(regs.EEPROM_ADDR, n)

    def fpga_bulk_write(self, data):
        """EP 0x02 OUT. Only callable inside the 'fpga' guarded context (see device.fpga_config)."""
        if 'fpga' not in self._ctx_guard:
            raise DscopeError('UNSAFE_OPERATION', 'EP2 OUT outside FPGA configuration')
        if self._fb_written:
            raise DscopeError('UNSAFE_OPERATION', 'EP2 OUT after an EEPROM address write (reopen first)')
        try:
            n = self.h.bulkWrite(EP_OUT, bytes(data), timeout=5000)
        except usb1.USBError as e:
            raise DscopeError('FPGA_CONFIG_FAILED', 'bitstream transfer failed: %s' % e)
        if n != len(data):
            raise DscopeError('FPGA_CONFIG_FAILED', 'bitstream short write %d/%d' % (n, len(data)))

    # ---------------------------------------------------------------- bulk IN
    def ep84_byte(self, timeout_ms=TIMEOUT_MS):
        if self.failed:
            raise DscopeError('DEVICE_ERROR', 'EP 0x84 read refused: the device failed earlier (%s)' % self.failed,
                              HINT_REOPEN)
        try:
            d = self.h.bulkRead(EP_CTRL_IN, 1, timeout=timeout_ms)
        except usb1.USBErrorTimeout:
            return None
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'EP 0x84 read failed: %s' % e)
        return d[0] if d else None

    def _hs_step(self, want, record):
        try:
            v = self.ep84_byte(HS_TIMEOUT_MS)
        except DscopeError:
            raise
        except BaseException:
            self.fail('EP 0x84 handshake interrupted')
            raise
        if record is not None:
            record.append(v)
        if self.log:
            self.log('EP84 -> %s' % ('timeout' if v is None else '%02X' % v))
        if v != want:
            why = 'EP 0x84 handshake: expected %02X, got %s' % (want, 'timeout' if v is None else '%02X' % v)
            self.fail(why)
            raise DscopeError('DEVICE_ERROR', why, HINT_REOPEN)

    @contextlib.contextmanager
    def handshake(self, record=None):
        """One register read while an acquisition runs (DSWave QueryPercent 0x34af00): EP 0x84 byte (expect 01),
        EP 0x84 byte (expect 02), the read, EP 0x84 byte (expect 03). A timeout or unexpected byte before the body
        means the body is NOT sent. If the read fails the closing byte is not read either (QueryPercent returns at
        once). Any failure puts the session in the error state and raises DEVICE_ERROR. `record` (list) receives
        every byte read (None = timeout)."""
        with self.lock:
            if self._in_hs:
                raise DscopeError('UNSAFE_OPERATION', 'nested EP 0x84 handshake')
            self._hs_step(1, record)
            self._hs_step(2, record)
            self._in_hs = True
            try:
                yield
            except BaseException as e:
                self._in_hs = False
                why = 'control transfer inside the EP 0x84 handshake failed: %s' % getattr(e, 'message', e)
                self.fail(why)
                if isinstance(e, DscopeError):
                    raise DscopeError('DEVICE_ERROR', why, HINT_REOPEN) from e
                raise
            self._in_hs = False
            self._hs_step(3, record)

    def handshake_writes(self, writes, record=None):
        """Register writes while an acquisition runs (DSWave RegQueueThread 0x34b8c0): 01, 02, every write, 03.
        As in DSWave a failed write does not abandon the handshake: the remaining writes are still sent and the
        closing byte is still read; the session is failed afterwards (VendorWrite sets the device error)."""
        with self.lock:
            if self._in_hs:
                raise DscopeError('UNSAFE_OPERATION', 'nested EP 0x84 handshake')
            self._hs_step(1, record)
            self._hs_step(2, record)
            first_err = None
            self._in_hs = True
            try:
                for addr, val in writes:
                    try:
                        self.write(addr, val)
                    except DscopeError as e:
                        first_err = first_err or e
                        if self.log:
                            self.log('queued write %02X=%04X failed: %s' % (addr, val, e.message))
            finally:
                self._in_hs = False
            try:
                self._hs_step(3, record)
            except DscopeError:
                if first_err is None:
                    raise
            if first_err is not None:
                why = 'register write inside the EP 0x84 handshake failed: %s' % first_err.message
                self.fail(why)
                raise DscopeError('DEVICE_ERROR', why, HINT_REOPEN) from first_err
