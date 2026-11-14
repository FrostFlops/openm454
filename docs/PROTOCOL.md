# DSCope M454 USB protocol ("v3", DSWave 1.2.1)

Recovered on 2026-10-31 from DSWave 1.2.1 (closed source) by static analysis (Ghidra), in-process SCPI injection
into DSWave's hardware layer and WinUSB tracing with Frida: a cold open, a warm open and ~125 scripted runs. The
recordings (they contain the device's security key), the decompiled code and the working notes are not published;
RVAs refer to DSWave.exe 1.2.1. What each recorded run configured is in `tests/fixtures/scenarios/*.json`. This file
is the authority for the driver in `openm454/`.

## 1. Identification

| Item | Value |
|---|---|
| VID / PID | 0x2A0E / 0x0080 |
| Strings | manufacturer `DreamSourceLab`, product `USB-based DSL Instrument v3`, no serial |
| Speed | SuperSpeed (DSWave also has a High-Speed profile for the same PID) |
| Driver | WinUSB (MS OS descriptors, no INF needed); one opener at a time |
| Interface | config 1, interface 0, alt 0, class 0xFF |
| Endpoints | 0x02 bulk OUT, 0x84 bulk IN, 0x86 bulk IN (all 1024-byte max packet) |
| MCU / FPGA | WCH CH569 (firmware 0x3110), panel MCU (0x3100), Xilinx Spartan-6 XC6SLX16 (bitstream 0x3100) |

## 2. Transport

* **Register write**: control OUT, bmRequestType 0x40, bRequest **0xB0**, wValue 0, wIndex 0, data `[addr, value & 0xFF, value >> 8]`.
* **Register read**: control OUT 0x40/**0xB1** data `[addr, len, ext]`, wait 10 ms, control IN 0xC0/**0xB2** wLength = len.
  `len` < 128; MCU registers (addr >= 0xF0) are read with len 2. `ext` selects a sub-index (probe number for 0xFA).
* **0xB3 = ISP firmware upgrade. Never send it.**
* **EP 0x02 OUT**: FPGA bitstream (after `BULK_SIZE_LOW/HIGH`), and EEPROM writes (after `EEPROM_ADDR`, `BULK_SIZE`).
  Both use the same mechanism, so the driver only writes EP2 inside FPGA configuration, as the first write of a session.
* **EP 0x84 IN**: one-byte handshake used to change registers while a capture streams (see §7).
* **EP 0x86 IN**: capture data (see §8).

## 3. Registers (8-bit address, 16-bit value)

### HW / base (0x00-0x0F)
| Addr | Name | Notes |
|---|---|---|
| 0x00 | HW_BASE_MODE | bit0 repeat, bit1 stream (sent while acquired), bit2 loop: 0 = single block, 1 = repeat (real-time frames), 2 = stream (one record, roll), 3 = repeat + stream, 6 = loop (endless stream, §8.1) |
| 0x01 | HW_BASE_CTRL | write: 1 = run, 0 = stop. read: bit1 SEC_READY, bit2 SEC_PASS (bit5 seen set after DSWave sessions) |
| 0x02 | HW_BASE_ACTION | 0 at start, 0x08 flow pause/stop, 0x10 resume, 0x04 abort/flush (after stop). read = progress |
| 0x03 | HW_BASE_SYNC | sync word written at start (DSWave uses 0) |
| 0x07 / 0x08 | AWG data / AWG address | indirect AWG access: W 0x08 = awg_addr, then W 0x07 = value (DSWave's log labels these swapped) |
| 0x09 / 0x0A / 0x0B | TRIG_ADDR / TRIG_DATA / TRIG_SHFT | indirect trigger memory |
| 0x0E / 0x0F | SEC_CTRL / SEC_DATA | security handshake |

### MSO (0x10-0x74)
HORI 0x10-0x1B (BASE, DEPTH0/1, RWORDS0/1, ADIVL/H, DDIVL/H, AMTXS, DMTXS, SLOWCNT), VERT 0x20-0x2B (DFOLD, DTHR,
DUP, DEN0-3, AFOLD, ACROSS, ABRH_INV, AWR), TRIG 0x30-0x3A (BASE, PRES0/1, PREW0/1, LEVEL, INDEX, MARGIN, HOLD0/1,
PULSE), per analog channel n at 0x40 + 0x10·n: BASE, VERT, VOFF, VPWM, CROSS. Formulas: §6.

### MCU (0xF0-0xFF)
| Addr | Name | Access |
|---|---|---|
| 0xF0 | FW_VERSION | R (0x3110) |
| 0xF1 | BIN_VERSION | R (FPGA, 0x3100; valid after configuration) |
| 0xF2 | PANEL_VERSION | R (0x3100) |
| 0xF4 | HARDWARE_INDEX | W 0 (DSWave `:Device:Index 0`) |
| 0xF9 | FLUSH_DATA | W 0 at the end of every setup |
| 0xFA | RATIO_INFO | R, ext = probe index (reads 0x3100 on this unit: no probe detection) |
| 0xFB | EEPROM_ADDR | W address, then R n bytes = EEPROM contents |
| 0xFC / 0xFD | BULK_SIZE_LOW / HIGH | byte count of the next EP2 OUT |
| 0xFE | PROG_B | W 0xFFFE = FPGA PROG_B low, 0x0001 = high |
| 0xFF | HW_STATUS | R: bit1 INIT_B, bit7 FPGA DONE |

## 4. Open sequence (cold and warm; `openm454/device.py`)

1. R 0xF0 (>= 0x3110), R 0xF2 (>= 0x3100). Older firmware would make DSWave run an ISP upgrade: the driver refuses.
2. Claim interface 0. R 0xFF.
   If bit7 (DONE) = 1, the device may still be running a capture an earlier session left behind (error stop, killed
   process). The driver stops it before anything else: W 0x01 = 0, 1 ms, W 0x02 = 4. This is DSWave's
   UsbDevice_Reopen 0x33eaa0 order: IsFpgaDone, then ForceAcquire(0). Then it waits 100 ms. This is best effort:
   a controller that no longer answers needs a power cycle.
3. If bit7 (DONE) = 0: FPGA configuration (always the first write of the session; an unconfigured FPGA cannot be
   running): W 0xFE=0xFFFE, ~10 ms, W 0xFE=1, poll R 0xFF bit1 (<= 11x),
   W 0xFC = len & 0xFFFF, W 0xFD = len >> 16, EP2 OUT the raw bitstream (466,408 bytes) in one transfer,
   poll R 0xFF bit7 (<= 11 x 50 ms). DSWave takes ~110 ms for this.
   Recovery (`openm454 recover`): the same configuration with DONE = 1, still as the
   first write of the session. The PROG_B pulse clears the FPGA as at power-up. Verified on hardware: open
   0.86 s, security passes again, selftest passes after it. The AWG output is **not** reset by it.
4. Wait 100 ms, R 0xF1 (>= 0x3100).
5. Security handshake (§5).
6. R 0xFA ext 0..3 (probe ratio), read calibration: 8 x (W 0xFB = 0x40·i, R 0xFB 64 bytes) = EEPROM 0x000-0x1FF.
7. Idle (InitMso 0x3497a0): W 0x01 = 0, W 0x02 = 8. Close sends nothing (UsbDevice_Close 0x33f300).

Warm open takes ~0.6 s (dominated by the 10 ms read delay).

### FPGA bitstream
Embedded in DSWave.exe 1.2.1 (SHA-256 `A21A1D41...ECA42C`) at file offset 0xC471E0, length 0x3E414, as a `DSLZ`
container: `b'DSLZ'`, u32 BE raw size (466,408), 32-byte SHA-256 of the raw data
(`f45a97ea44ffda110cffc659eaba250d191985f7e60fdfcbd153b57372e9cfa1`), then one LZ4 block.
The raw image starts with 16 x 0xFF and the byte-swapped Spartan-6 sync word `55 99 AA 66`.
`openm454/firmware.py` extracts and verifies it from a local DSWave install; it is never redistributed.

## 5. Security handshake

1. W 0xFB = 0x1000, R 0xFB 16 bytes = key, 8 little-endian u16 words (blank = all 0xFFFF: DSWave would then
   write a new key; the driver refuses instead).
2. W 0x0E = 0, wait 10 ms, W 0x0E = 1. R 0x01: if bit2 (PASS) is already set, done.
3. W 0x0F = 0, W 0x0E = 0x0513.
4. For each key word **in reverse order (word 7 first)**: poll R 0x01 until bit1 (READY), R 0x0F (challenge, unused
   when the key is not blank), W 0x0F = word, W 0x0E = 0x0219.
5. R 0x01: bit2 (PASS) must be set. Without PASS the FPGA still answers, but DSWave treats it as a failure.

## 6. Capture configuration

DSWave's HAL is configured with SCPI-like commands (`:ANALOG:CHANNEL:Vdiv ch,uV`, `:SAMPLEtime ps`,
`:STORAGEdepth S`, `:TRIGger:...`; the vocabulary is DSWave.exe's SCPI token table) and turns them into a
register program at `:Work:RUN 0,0`. The driver reproduces that program (`openm454/mso.py`, golden-tested
against the recorded runs). Key facts:

* **Channel enable**: ANAn_BASE bit0 = enable, 0x4 = DC (AC clears it), 0x8 = 20 MHz limit.
  VERT_AFOLD = (n_enabled << 8) | log2(fold), analog fold x4 / x2 / x1 for 1 / 2 / 3-4 channels.
  VERT_DEN0 = digital enable mask, VERT_DFOLD likewise with digital fold x4 / x2 / x1 for 1-2 / 3-4 / 5-8 channels.
* **Rates and depth**: S = STORAGEdepth. Base rate = min(100 MHz, S/4 / window) snapped down to 1-2-5
  (1 kHz .. 100 MHz); analog rate = base x afold (max 400 MSa/s), analog depth = S/4 x afold;
  digital likewise with dfold. HORI_ADIV/DDIV = rate dividers (100 MHz / base). So one analog channel offers
  4 x {1, 2, 5} x 10^n (80 and 200 MSa/s, never 100). With a fixed S a snapped-down rate lengthens the window
  (up to 2.5x); the driver instead lowers S to rate x window so the requested window is kept (`scope.hal_config`).
* **Vertical**: ANAn_VERT = calibrated gain code for the V/div (bit15 = attenuation relay, set from 200 mV/div up;
  the only relay click across the range), ANAn_VOFF = offset code (ADC code of 0 V, 128 = centre),
  ANAn_VPWM = offset DAC value from calibration and offset. 235 ADC codes = 8 divisions.
  VERT_ABRH_INV = per-channel invert mask XOR the hardware's own inversion (0x0F).
  ANAn_CROSS = 0xFFFF followed by a 256-entry table per channel (interleave correction); **must be rewritten
  whenever the enabled-channel set changes** (stale tables corrupt levels).
* **Digital threshold**: VERT_DTHR = int(1023 x (volts / 5) x 25/33).

Exact per-register formulas (verified write-for-write against 123 DSWave runs; implementation, with the RVA of
every ported function, in `openm454/mso.py`):

| Register | Formula |
|---|---|
| ANAn_BASE | enable \| 4·(DC) \| 8·bw20 (written for all 4 channels, enabled or not) |
| ANAn_VERT | ((v32 >> 16) << 15) + (v32 & 0x7FFF), v32 = gain \| relay << 16 from the channel's calibrated VGA entry |
| ANAn_VOFF | offset code |
| ANAn_VPWM | int(((corr + v) / 255) · pwm_scale) + pwm_offset[vdiv]; v = voff, or 255 - voff if the channel is inverted (hardware inverts all four, XOR user invert); corr = 0 except with exactly one analog channel (ADC trim) |
| VERT_ACROSS | table indexed by the enabled-analog mask: {0:F,1:F,2:F,3:A,4:F,5:5,6:5,7:0,8:F,9:5,10:A,11:0,12:A,13:0,14:0,15:0} |
| HORI_xDIVL/H | div = ceil(fold_factor · 100 MHz / rate); L = div & 0xFFFF, H = div >> 16 |
| HORI_BASE | 2·analog_primary + 8·small_packets + (trigger on analog) + 4·!((nD and analog_primary) or nA == 0) |
| HORI_SLOWCNT | (4096 - block_bytes) >> 2 |
| HORI_DEPTH0/1 | hw_depth >> 6 (lo/hi), hw_depth = roundup4096(fold_trig · hori_depth + trig_block) |
| HORI_RWORDS0/1 | rd_bytes / 4 - 1 (lo/hi) |
| TRIG_BASE | 2·auto + 4·pulse_type + enable |
| TRIG_PRES / PREW | pretrig = B · (SAMPLEindex // B + 1), B = 64 · mtxs of the trigger kind; PREW = B · count / 8 |
| TRIG_HOLD | holdoff / 10 ns |
| ANAn_CROSS | 0xFFFF, then 256 entries clamp(int(j + base + j·k + 0.5)) from the zero-info cross pair (a, b): k = (b - a) / (225 · 5), base = a / 5 - 15 k |

Geometry: block = primary kind's 64·mtxs samples + secondary kind's samples; packet sizes 1,048,320 (1 MiB - 256,
whole blocks) for fast captures; slow data rates (< 102,400 kB/s) use "roll" packets of ~20 ms of data, MODE 2, and
up to 64 queued transfers; repeat mode (MODE 1) delivers one frame (data + 4 KiB tail) per transfer.
* **Trigger**: TRIG_BASE bit0 = enable, bit1 = auto (free-run; normal mode clears it), bit2 = pulse trigger.
  Source and edge go into the trigger memory: TRIG_INDEX = 0x8000 | probe; then indirect words
  0x000 (level mask = ~(1 << bit)), 0x010 = 0xFFFF, 0x100 (polarity: bit set for rising), 0x200 (edge mask = 1 << bit),
  0x400/0x401 (pulse width), then TRIG_SHFT = 0. Analog channels use bits 0-3, digital channels bits 4-11.
  Types: 0 rise, 1 fall, 2 any, 3..6 pulse variants (TRIG_PULSE = 3, 1, 2, 0). TRIG_LEVEL = 8-bit ADC code.
  TRIG_PRES/PREW = pre-trigger size; TRIG_MARGIN = noise margin 0..15; TRIG_HOLD = holdoff in 10 ns units.

## 7. Run / stop

Implementation (with the RVAs): `openm454/acquire.py`.

1. At least 100 ms after the previous stop. Setup program: W 0x01 = 0, 1 ms, MSO writes, W 0x00 = MODE,
   W 0xF9 = 0. All of these are direct control transfers.
2. Queue bulk reads on EP 0x86 **before** starting: one 4096-byte head, then N data transfers of `pkt_bytes`
   (N = 16 on USB3, 2 in repeat mode, up to 64 in roll mode; `pkt_bytes` = 1,048,320 for most single
   captures). There is no timeout on these reads. Wait 50 ms.
   The driver pads every data read to a multiple of 1024 bytes on every OS except repeat mode (1,048,576 instead of
   DSWave's 1,048,320). Windows and macOS keep DSWave's N. On Linux the queue also has to fit the usbfs buffer:
   N = min(geometry N, (limit*MiB - 4608 - 2048) // (pkt + 512 + 40*ceil(pkt/16384))), which is 15 at the stock
   16 MiB for 1 MiB reads (each read is charged its length + ~300 B of URB bookkeeping, plus one 32 B scatterlist
   entry per 16 KiB; 4608 = head + its charge, 2048 = reserve for the one control or EP 0x84 transfer in flight).
   The lowered N is accepted only when it reaches the floor: at least 4 reads, or at least 2 reads that hold 4 MiB
   (4 reads of ~1 MiB, 3 of ~2 MB, 2-3 of a live stream's 4-10 MB). Below it the run is refused with `USB_NO_MEM`
   before the setup program. A libusb NO_MEM at submit keeps the reads already queued when they reach the floor
   (`acq.transfers` is then lower than `acq.transfers_wanted`); otherwise all are cancelled and the error is
   `USB_NO_MEM`. A NO_MEM on a resubmit during a run is `DEVICE_ERROR`: the scope is left running and the next
   open stops it.
3. Start: W 0x03 = sync (0), W 0x02 = 0, W 0x01 = 1, 1 ms.
4. **While running** (from W 0x01 = 1 until the stop handshake has completed), every control transfer must be
   inside an **EP 0x84 handshake**. Read 1 byte from EP 0x84 and expect 01. Read another and expect 02. Then send
   the control transfers. Read one more and expect 03. Each read has a 1000 ms timeout. DSWave never sends a
   control transfer while running outside this pattern (1800 EP 0x84 bytes in the corpus, all 01/02/03 triples).
   A failed EP 0x84 read abandons the handshake: nothing more is sent. A failed register write inside a write
   handshake does not: RegQueueThread 0x34b8c0 logs it, sends the remaining writes and still reads 03; the device
   is in error afterwards. A failed read in a progress poll skips 03 (QueryPercent returns at once).
   The B1 -> B2 read delay is nominally 10 ms, which lasts 13-31 ms on the Windows timer tick. The driver uses
   15 ms (never below 12.95 ms in the 470 recorded polls).
5. While the head has not arrived (not in repeat mode), poll progress from 100 ms after the start, about every
   15 ms. Each poll is a handshake around R 0x02. The value v gives n = (v & 0xFFFE) << 13 samples
   (v & 1: hw_depth - n). Normal mode waits until the user stops; auto mode delivers the head after ~15 ms.
6. The head arrives when the capture triggers, followed by the data. **Every completed data transfer is
   resubmitted for the whole run**, even after the record is complete: the device streams zero padding until
   stopped. Stopping the reads early back-pressures the MCU, and the stop handshake then hangs. That is the
   likely cause of the 2026-10-31 wedge.
7. Normal stop (frame complete, timeout, or user stop): a handshake around W 0x01 = 0, W 0x02 = 8. Then stop
   resubmitting, cancel every outstanding read (head included) and wait until all are back. Then
   W 0x01 = 0, 1 ms, W 0x02 = 4. Then nothing for 100 ms (FUN_14033f270 sleeps 100 ms before anything else).
   **(hardware, 2026-10-31)** After the stop that ends a run without a trigger, register writes sent within
   ~10 ms are acknowledged on USB but dropped by the device (an AWG program right after a normal-mode timeout was
   lost in 3 of 3 rounds; with 10-50 ms of delay it applied every time; after triggered or forced runs it always
   applied). The driver holds every register access for 100 ms after the stop (`Transport.hold`).
   Front-end settling (hardware, not protocol): after a run that saturated an analog input (or ended without
   data) the next run waits 0.3 s after its setup writes (1500 div of overdrive at 1 mV/div needed ~215 ms to
   settle within 0.5 LSB); the first analog run after a run without analog channels (or of a session) waits
   0.15 s (the inputs start 8-14 codes high and settle in ~80 ms). `Engine.run(settle=...)`.
8. Error stop (failed handshake step, failed control transfer, bad head, transfer status other than
   COMPLETED/CANCELLED, zero-length data transfer, failed resubmit): send **nothing** more, not even EP 0x84 reads.
   Cancel the reads, wait for them, and leave the device until it is reopened. DSWave's close sends nothing either.
   Priority (TransferThread 0x33f950): frame complete or abort is handled **before** an error that arrived with
   it. Such a run gets the normal stop handshake and drain, but not the final W 0x01 / W 0x02 = 4 (ForceAcquire is
   refused once the device is in error).
   A run is complete only once the 4 KiB tail has arrived, at acqu_bytes + 4096 data bytes. A triggered
   (non-roll) head arrives only after the whole record has been acquired, so trigger waits must include the record
   time.
9. If the controller stops answering 0xB2 reads, nothing on the host recovers it: pipe resets and device resets
   don't help, and DSWave has no reset path. Recovery is a power cycle (replug). `openm454 recover` (§4) needs
   working register reads, so it can only help when the FPGA side is stuck but the controller still answers; it
   has not yet been tried on a hung device.
10. **(hardware, 2026-10-31)** With the rules above, the driver ran the configuration that wedged the old engine
   (A1 + D0 at 400 MSa/s each, normal trigger, including runs without a trigger) without a problem, as well as
   12/12 selftest checks, 11 hardware tests, watch, stream and stop requests.

## 8. Data stream

* **Head** (4096 bytes): u32[0] = 0x55555555; u32[2]: bit31 set = not triggered (free-run), otherwise the trigger
  sample = (u32[2] & 0x3FFFFFFF) + pre-trigger offset (block-rounded `SAMPLEindex`).
* **Single mode, 4 analog + 8 digital**: 320-byte blocks of 64 samples: 256 analog bytes = 8 groups of
  [A0 x8][A1 x8][A2 x8][A3 x8] (8-bit codes), then 64 digital bytes = 8 channels x u64 LE, bit i = sample i.
  Other channel sets / folds: see `openm454/packet.py`.
* Only the first N samples (`MSO: send samples: N`) are valid; the device keeps streaming until stopped.
* **Volts** = (code - VOFF) x V/div / 29.375 x probe factor; higher code = higher voltage.

### 8.1 Stream (MODE 2) and loop (MODE 6) throughput (hardware, 2026-11-01)

Measured with every input on a ~1 kHz square; a lost or overwritten packet shows as a phase step of the square.

* **The 102,400 kB/s switch is a HAL policy** (M454 profile `hw+0x68`; 10,240 on USB 2), read only by the geometry
  (0x3512f0). Below it DSWave streams in MODE 2 with packets of 20 ms of data and ~200 ms of reads queued; at or
  above it, it records in MODE 0 and reads 1 MiB packets afterwards. Forced into MODE 2, 200 MB/s still streamed
  in real time; at 400 MB/s the readout ran at 270-290 MB/s and the record arrived complete and continuous (the
  DDR buffers one record). A finished MODE 0 record reads out at 175-265 MB/s depending on the channel set (1 analog
  at 400 MSa/s: 250-265; 2 analog + 8 digital at 100 MSa/s: 214; 4 analog + 8 digital at 50 MSa/s: 175). Every MODE 0/1/2 record is bounded
  by `rd_bytes <= 512 MiB`; chaining MODE 2 records costs ~0.18-0.22 s per seam. Measured between two 1 s records
  (8 digital at 100 MSa/s): 43 ms until the last packet and the tail arrive, 3 ms stop handshake and cancel, 100 ms
  quiet after the stop, 19 ms setup writes (62, including 15 ms after CTRL = 0), 4 ms to queue the reads, 50 ms
  before the start: 223 ms. A record cannot be extended: the FPGA stops at its end and has to be set up again.
* **Loop mode** (`:LOOPModeEnable 1`; DSWave's Data Acquisition / 记录仪 mode) is an endless stream: the setup of
  a MODE 2 run with trigger off and explicit rates (AUTORATE off; `:SAMPLEtime` is ignored), fold 0
  (VERT_AFOLD / VERT_DFOLD = 0x100 x channels, so at most 100 MSa/s per channel), HORI_RWORDS = one packet - 1
  word, HW_BASE_MODE = 6. DSWave always uses fold 0 in loop mode, but the FPGA also interleaves there exactly as in
  a capture (hardware, 2026-11-02): with the normal VERT_AFOLD / VERT_DFOLD and dividers, 1 analog channel at 200
  and 400 MSa/s, 2 analog at 200, 2 digital at 400, 4 digital at 200 and mixed sets streamed at the right rate,
  without gaps and with the same per-phase pattern as a 400 MSa/s capture (plateau means of the four phases
  165.32 / 165.81 / 165.34 / 165.01 codes vs 165.28 / 165.81 / 165.36 / 165.02). After the 4096-byte head (u32[2] = 0xC00000nn) the device sends plain blocks until the
  normal stop: no tails, no markers, every read full. Packets as in MODE 2 (20 ms of data, or floor(1 MiB / block)
  blocks with 16 reads at >= 102,400 kB/s); the host may read in larger multiples.
* **Rates are fold x 100 MHz / N for any integer N** (HORI_ADIV / HORI_DDIV: the low register holds N's low 16
  bits, the low byte of the next one its high 8 bits; its high byte (an "m" prescaler) is always 0 in DSWave).
  DSWave only uses its 1-2-5 table. Verified in loop mode on the calibration square (hardware, 2026-11-02): N = 1-13,
  16, 25, 37, 64, 99, 128, 333, 997, 1000, 1001, 2268, 4999 and 10000 sample at exactly 100 MHz / N (period ratios
  within 0.06 %); N = 70000 (> 16 bits) runs near its 1.43 kSa/s, not at the 22 kSa/s its low 16 bits alone would
  give. DSWave's HAL derives N as ceil(fold x 100 MHz / rate), so a rate in whole Hz that is not exact (33,333,333
  for 100 MHz / 3) becomes N = 4; an earlier note here that "N = 3 samples at 25 MSa/s" was that rounding.
* **Small packets** (20 ms of data <= 1023 bytes, i.e. below ~51 kB/s; HORI_BASE bit 3): each 4096-byte read
  carries one block (64 samples per channel) followed by 4032 bytes of filler; one read per block period.
* **Nothing is read out after a stop.** After the stop handshake (CTRL = 0, ACTION = 8) the scope sends nothing
  more, and the same after a handshake with CTRL = 0 alone (tested with ~250 MB in the ring at 400 MB/s): whatever
  is still in the ring is lost. After CTRL = 0 alone the EP 0x84 handshake stops answering (byte 02 times out); the
  next open recovers. So a loop run must keep acquiring until the host has read everything it wants. A burst of T s
  at data rate D, read out at R < D, takes D x T / R to read and ends with a backlog of (D - R) x D x T / R, so
  T <= ring x R / (D x (D - R)). A record (MODE 0 / 2) instead stops acquiring at its end and is read out
  afterwards: T <= 512 MiB / D (and 400M samples per channel). At 500 MB/s with R = 200 MB/s: loop 0.72 s with the
  whole 512 MiB, record 1.0 s; at 400 MB/s with R = 275 MB/s: loop 2.95 s, record 1.34 s (loop is longer when
  R > D / 2, the record when R < D / 2).
* **The 512 MiB DDR is a ring.** The host may fall behind by up to ~512 MiB without loss (host stalls of up to 10 s
  at 50 MB/s were absorbed and caught up). Past that the device overwrites unread data without any flag and keeps
  streaming: one phase step on analog, and the digital channels decode as garbage (the block alignment is lost)
  until the run is restarted. The break came at 508-511 MiB (2026-11-01, a 12 s host stall and a 250 MB/s run) and
  at ~514 MiB (2026-11-02, 250 MB/s, 8.88 s into the run) of the host's estimate backlog = rate x (t - t_head) -
  bytes received, i.e. at the ring size. The host must track it; over long runs it needs a baseline for the scope /
  PC clock difference (the driver lets the baseline follow raw down at once and, while the host is caught up, up
  by at most 200 ppm x rate per second), checks it on every transfer and stops at 480 MiB: two 250 MB/s runs
  stopped there were complete to their last sample.
* **Readout while acquiring: ~196-275 MB/s, depending on the channel set** (the read size, 1 to 8 MiB, makes no
  difference). 4 analog + 8 digital at 50 MSa/s (250 MB/s) read out at ~200 MB/s, so the backlog grew ~55 MB/s and
  the ring overflowed after ~10 s; 2 analog + 8 digital at 100 MSa/s (300 MB/s) read out at 266 MB/s; 2 analog at
  100 MSa/s (200 MB/s) ran 20 s without backlog in one run and ~2 % short in another; 1 analog at 200 MSa/s
  (interleaved, 200 MB/s) ran 10 s without backlog. With the same interleave the readout is the same in MODE 2 and in
  loop mode (4 analog + 8 digital at 50 MSa/s: 205 / 206 MB/s; 4 analog at 100 MSa/s: 276 / 275 MB/s); it depends on
  the interleave: 2 analog + 8 digital at 100 MSa/s read out at ~205 MB/s in MODE 2 (two converters per analog
  channel) and at 266 MB/s in loop mode (one converter per channel). Gap-free loop runs: 4 analog
  + 8 digital at 50 kSa/s (60 s) and at 1-20 MSa/s (100 MB/s), 8 digital at 100 MSa/s, 2 analog + 8 digital at
  50 MSa/s (150 MB/s for 180 s, 27 GB, host lag constant), 4 analog + 8 digital at 1 MSa/s for 10 min (3 GB,
  599,361 periods, no step above 1.1 us).
* **MODE 2 above the switch** (this driver's `stream` default): 12 channels at 50 MSa/s (250 MB/s), ~2 s records
  sent while acquired: 0.74 / 0.66 s from one record to the next, records complete and continuous; the same
  records in MODE 0: 2.35 / 2.45 s.
* **Gap between two runs started back to back** = drain + restart. The restart (stop handshake, 100 ms quiet, setup,
  50 ms before the start) takes ~0.18 s. The drain is the time the host still needs, after the run's signal has
  ended, to read the data that belong to the run:
  * loop mode: T x (D / R - 1) for a run of T seconds at data rate D read out at R while acquiring (0 when R >= D).
    The scope keeps acquiring during the drain; what it acquires after T is never read (dropped at the stop).
  * MODE 0 (DSWave's choice at or above 102,400 kB/s): the whole record, D x T / R, because nothing is sent while
    the record is acquired.
  * MODE 2 below 102,400 kB/s: ~0 (sent while acquired).
  Measured with 4 analog + 8 digital channels (identical programs in both modes apart from the mode): at 20 MSa/s
  (100 MB/s, 3 s runs) loop 0.178 s, MODE 2 0.191 s; at 50 MSa/s (250 MB/s, 1.5 s runs) loop 0.51 s (drain 0.33 s),
  MODE 0 2.33 s (drain 2.14 s: the 375 MB record read out after it was acquired), MODE 2 forced above the switch
  0.73 s (drain 0.54 s).

## 9. AWG (MCX output 0..3 V)

Indirect: W 0x08 = awg register, W 0x07 = value. `openm454/awg.py` matches all 13 recorded DSWave programs.

| AWG reg | Meaning |
|---|---|
| 0 WAVE | 0 sine, 1 triangle, 2 square, 3 noise |
| 1 MODE | 0 basic (others: modulation, sweep, burst; not implemented) |
| 2 SWITCH | 1 ... 0 brackets mode / wave / frequency updates |
| 3 ENABLE | 2 = armed, 3 = output running (start = SWITCH 1, ENABLE 3, SWITCH 0; stop = ENABLE 2). Stopped, the output holds the LOW level; the driver's `awg off` also writes LOW = 0 so the output sits at 0 V (hardware, 2026-10-31) |
| 4/5 FCW | trunc(f_Hz x 40) (f from the micro-Hz SCPI value) |
| 6/7 PHASE | trunc(whole_degrees / 360 x 4e9) (DSWave drops the fraction); relative to the restart of the generator at each program |
| 8/9 HIGH/LOW | trunc(uV / 6e6 x 0.91525 x 1023) (~156.05 codes per volt) |
| 10/11 DUTY | fraction x 4e9 |
| 12/13 RSLOPE/FSLOPE | DUTY32 = trunc(duty x 4e9); RSLOPE = trunc(65472 / ((DUTY32 >> 22) & 0x3ff)), FSLOPE = trunc(65472 / ((4e9 - DUTY32) >> 22)) |
| 14-17 RPAR/FPAR | trunc(3846153.0 / f_Hz + 0.5); 4 in sweep mode |

Register names 0x00-0x66 (modulation, sweep, burst, trigger) are listed in `openm454/awg_modes.py` (`REG_NAMES`).
The formulas above are DSWave's exact ones (`openm454/awg_modes.DswaveAwgHal` replays its AWG command handler and
reproduces every recorded program write for write).

Measured output behaviour (2026-10-31, A0 loopback): stopped (ENABLE 2) the output holds LOW, so `awg off` writes
LOW = 0 and `awg dc` writes LOW = level; edges ~155 ns 10-90 %; the generator runs from the scope's clock (< 0.07 ppm
apart); its 0 V reads +5 mV; noise mode is Gaussian-like around (low+high)/2 with sigma ~2.8 % of (high-low), flat to
~2.5 MHz (frequency ignored).

### 9.1 Modulation, sweep, burst (MODE 1 / 2 / 3)
Register programs derived from the decompiled HAL and
confirmed against DSWave for the 73 scenarios of `tests/fixtures/scenarios/corpus5.json` (`tests/test_awg_modes_golden.py`,
fixture `tests/fixtures/awg_modes_programs.json`). Implementation: `openm454/awg_modes.py` (`program_modulation`,
`program_sweep`, `program_burst`), started with the basic START. Verified on A0:
* Sweep: linear 1 -> 10 kHz in 10 ms ramps at 0.9 MHz/s and restarts every 10 ms; log 1 -> 100 kHz in 100 ms has
  d(ln f)/dt = 46.05 /s as programmed. Steps every TINC x 20 ns.
* Burst (internal trigger): N carrier cycles every period (3 x 10 kHz every 1.000 ms).
* AM: DSWave's depth D = (Amax - Amin) / Amax (classical index D / (2 - D)); FM: the deviation is the peak
  deviation (2 kHz -> 8..12 kHz); FSK: the hop frequency is absolute (alternates 10 / 20 kHz), keyed at the
  modulation frequency. PM, ASK, PSK exist in the HAL only (the GUI offers AM, FM, FSK) and are unverified.

## 10. Calibration ("zero info")

EEPROM 0x000-0x1FF, 4 channel records of 128 bytes: u16 0x0CAB tag, a per-channel offset-DAC span at byte 5,
12 per-V/div entries `{u16 pwm_base, u16 gain_code, u16 relay}` from byte 10 (gain codes equal DSWave's VGA table:
1 mV 0x31, 2 mV 0x11, 5 mV 0x10, 10 mV 0x00, 20 mV 0x18, 50 mV 0x02, 100 mV 0x1A, 200 mV 0x10001, 500 mV 0x10000,
1 V 0x10018, 2 V 0x10008, 5 V 0x1001A), then 8 signed trims, 0xFF padding. The driver only reads it.
