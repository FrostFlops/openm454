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
  The limit bit is all DSWave programs for the limit (the HAL uses the flag nowhere else). What it does depends
  on the number of enabled analog channels (measured 2026-11-25, every range): -3 dB at about 16.5 MHz with one
  channel, 24.5 MHz with two, above 30 MHz with three or four (-0.9 dB at 20 MHz), and it lowers the gain at
  every frequency including DC by 2.2 % / 1.1 % / 0.6 %. Every interleave phase is attenuated alike and the
  limit bits of the other channels have no effect.
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
   applied). After the stop of a run that ended without a head (a normal/auto timeout with no trigger, or a stop
   request before the trigger), and after every repeat run (the device re-arms after every frame and is never told
   the count, so the stop can find it armed for the next one), the driver always holds every register access for
   100 ms (`Transport.hold`, `QUIET_AFTER_UNTRIGGERED_STOP`), whatever the profile of 7.1; the `quiet` key shortens
   only the hold after forced or triggered single runs, because every floor measurement was one.
   Front-end settling (hardware, not protocol; the same in every profile): after a run that saturated an analog
   input (or ended without data) the next run waits 0.3 s after its setup writes (1500 div of overdrive at 1 mV/div
   needed ~215 ms to settle within 0.5 LSB); the first analog run after a digital-only run (or of a session) waits
   0.15 s (the inputs read 8-14 codes high and settle in ~80 ms). `Engine.run(settle=...)`.
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

### 7.1 Capture spacing (`OPENM454_TIMING`)

The waits in this section are DSWave's: reproducing them is how the driver keeps its fidelity without measuring
anything. A session may shorten selected waits with an environment variable; the traced values stay the baseline,
and nothing changes without the variable. The floor profile measured 35 ms per capture (p50) on one Windows bench,
against 193 ms with the traced spacing:

```bash
OPENM454_TIMING=gap=0.005,quiet=0.005,setup=0.002,submit=0.005
```

| key | wait (`openm454` constant) | traced | a profile may set |
| --- | --- | ---: | --- |
| `gap` | before every run (`GAP_BETWEEN_RUNS`) | 100 ms | **5 ms** .. traced |
| `quiet` | no register access after the stop of a single run that had a head, i.e. forced or triggered (`QUIET_AFTER_STOP`) | 100 ms | **5 ms** .. traced |
| `submit` | reads submitted -> start writes (`SUBMIT_TO_START`) | 50 ms | **5 ms** .. traced |
| `setup` | after `W CTRL=0` (`SETUP_SETTLE`) | 15 ms | **2 ms** .. traced |
| `final` | `W CTRL=0` -> `W ACTION=4` (`FINAL_GAP`) | 1 ms | **1 ms** (not shortened) |

Not adjustable, whatever the profile (a profile naming `start`, `read`, `wake` or `overdrive` is `INVALID_ARG` with a
hint saying why):

| wait | traced | why |
| --- | ---: | --- |
| after `W CTRL=1` (`START_SETTLE`, key `start`) | 15 ms | at 2 ms on its own, 1 of 300 forced captures came back with the settings of the capture before (table below) |
| per register read (`READ_DELAY`, key `read`) | 15 ms | at 2 ms on its own, the session ended in a STALL of the data transfer at capture 70 |
| analog front-end settling (`wake`, `overdrive`) | 150 / 300 ms | device physics: the inputs read 8-14 codes high for ~80 ms after a digital-only run (or at session start), and need ~215 ms after a saturated input |
| hold after the stop of a run without a head, and after every repeat run (`QUIET_AFTER_UNTRIGGERED_STOP`) | 100 ms | writes sent within ~10 ms of such a stop were acknowledged but dropped (item 7 above) |

The profile is refused with `INVALID_ARG` (exit 2, one JSON document) before anything is sent or the device is
opened, for every CLI command that can reach the device (`info`, `capture`, `watch`, `stream`, `log` including
`--plan`, `bench` including `--no-device`, `awg`, `release`, `recover`, `selftest`, `debug` including `plan`, `cal import --bind`), and the
MCP tools report `INVALID_ARG`: an entry that is not `key=value`, an unknown key, `start`, `read`, `wake` or
`overdrive`, a key given twice, a value that is not a finite non-negative number, a value above the traced one or below
its floor. A refused profile is never applied in part. Every `Scope()` reads `OPENM454_TIMING` again: a changed value
is applied, an invalid one is refused, and what is reported always equals what is applied (keys left out are at their
traced values).

**Why the floors, and why they are exactly these.** A shortened wait can make the device miss register writes
without any error: the capture then runs with the registers of the capture before, while the host scales it with the
new settings. Measured on one Windows bench (2026-11-17, one unit, fw `0x3110`, a 100 kHz square 0-2.8 V from the
generator into A0). Every capture alternates two settings - 10 MSa/s requested (8 MSa/s applied) at 1 V/div for
200 us, and 2 MSa/s at 0.5 V/div for 1 ms - so a capture taken with the other setting's registers reads 4x (or 1/4)
the frequency and 2x (or half) the Vpp:

| profile (keys not named: traced) | forced captures with a wrong reading | p50 per capture |
| --- | ---: | ---: |
| none (the traced spacing) | 0 of 299 | 193 ms |
| `gap=0.005,quiet=0.005,setup=0.002,submit=0.005,start=0.002,read=0.002` (the floors first proposed) | 14 of 300 and 17 of 300; the next phase ended in `DEVICE_ERROR: EP 0x84 handshake: expected 02, got timeout` | 23 ms |
| the same with `gap` and `quiet` at 100 ms | 8 of 300 | 123 ms |
| the same with `setup` at 15 ms | 10 of 300 | 37 ms |
| the same with `submit` at 50 ms | 4 of 300 | 70 ms |
| `gap=0.005,quiet=0.005` | 0 of 300 | 95 ms |
| `setup=0.002` | 0 of 300 | 181 ms |
| `submit=0.005` | 0 of 300 | 148 ms |
| `start=0.002` | 1 of 300 | 183 ms |
| `read=0.002` | `DEVICE_ERROR: data transfer ended with status STALL` at capture 70, the session lost | - |
| **the floor profile** `gap=0.005,quiet=0.005,setup=0.002,submit=0.005` | **0 of 3000** (and 0 of 300 normal-triggered) | **35 ms** |

With the floor profile, also all correct: 20 normal-mode timeouts each followed at once by a new AWG program and a new
V/div, 20 auto-mode fallbacks, and 20 untriggered repeat runs (50 frames each) each followed by a capture. The scope
answered a fresh open after every failed session above (no replug).

Earlier runs: a second bench ran the first proposed floors for 10,000 forced captures on Linux without a failure, with
one setting throughout (which cannot show a capture taken with the registers of the capture before). Zeroing `final`
wedged the scope within 2-3 captures there (Linux and Windows): `DEVICE_ERROR: EP 0x84 handshake: expected 02, got
timeout`, then no register answer until a replug; `final` is the stop handshake itself and is not shortened. On
2026-11-14 the profile `gap=0,submit=0,setup=0,start=0,read=0` (`final` 1 ms) gave `DEVICE_ERROR` from capture 19 on
the first bench, and needed a replug.

`openm454 info` (top-level `timing`, `{}` with the traced spacing), every capture's metadata and the capture/stream
JSON (`acq.timing`), the `log` result document - the last line `log` prints, also in its error document - and every
saved log file (the RAM `.npz`, each disk chunk, the meta of `index.json`; `acq.timing`, the same place as captures),
`watch` (`timing` in its first and its summary line), the selftest result (top-level `timing`) and the MCP summaries
(capture, analyze, record_stop, record_status keep `acq.timing`; status and selftest keep `timing`) report the
active profile, only while one is active. `log --plan` validates and applies the profile: in its stream comparison
the seam between two records (`gap_s`) is the drain, record x (D - R) / R, plus the restart, and the restart is
0.18 s with the traced spacing and 0.027 s at the floor profile (`max(gap, quiet)`, `setup` and `submit` shortened).

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
  `tests/fixtures/dswave/loop_mode_setup.json` is the first recording of DSWave 1.2.1's loop-mode (Recorder,
  HW_BASE_MODE 6) setup block and the first setup-program golden that runs without local traces, in CI;
  `tests/golden/test_mso_golden.py` already pins MODE 0/1/2 against the local recorded runs, which CI skips. The capture holds
  only the bRequest 0xB0 control writes (2026-11-16). Its `config` is reconstructed from those writes, using the
  Recorder's settings: AUTORATE off, analog = digital rate 1 MSa/s, trigger off, LOOPModeEnable 1, storage depth
  4,000,000 (the OSC default, which the HAL keeps), no window; `:SAMPLEtime` is ignored in loop mode, so any window
  gives the same program. The writes that only echo config inputs are listed in the fixture under `echoes`.
  `tests/golden/test_golden.py` compares 57 of the 61 writes exactly and the four VPWM words (W 0x43/0x53/0x63/0x73)
  by address only: VPWM holds the recorded unit's per-unit PWM calibration (its EEPROM zero info), which is not
  available.
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

### 8.2 Repeat mode (MODE 1 and 3): geometry, verified against DSWave 1.2.1

`openm454/mso.py` derives the repeat branch from the same functions as the rest of the geometry. Checked against
DSWave.exe 1.2.1 instruction by instruction (2026-11-16):

| field | DSWave 1.2.1 | `mso.geometry()` |
| --- | --- | --- |
| `pkt_pri_samples` (mso+0x118) | `Mso_ComputeGeometry 0x351686`: int(pri_samples x n_blocks) | same |
| `pkt_bytes` (mso+0x120) | 0x3516cd: `blk x nb`; 0x3516e6 rounds it up to `DEPTH_ALIGN_MASK` | `roundup(blk * nb, mask)` |
| `small_pkt` (mso+0x129) | 0x3516fa: cleared in repeat mode | `small_pkt = False` |
| `rd_bytes` (mso+0x140) | 0x351735: `blk x nb` | `blk * nb` |
| `transfer_size` | `UsbBsp3x_GetDataTransferSize 0x349b00`: `pkt_bytes + 0x1000` | `pkt_bytes + BYTES_PER_FRAME` |
| `transfer_count` | `UsbBsp3x_GetDataTransferCount 0x349bd0`: roll -> time-based, at most 64; repeat -> 2; otherwise 16 on USB 3 and 4 on USB 2 | same order |
| `hw_mode` | `UsbBsp3x_vt158 0x34a7a0`: `repeat + loop*4 + (loop ? 2 : roll*2)`: in loop mode bit 1 is forced and roll is not consulted, so loop gives MODE 6 (repeat clear) | same, `mso.geometry()` |

Each data transfer carries exactly one frame: the data of the record plus its own 4096-byte tail, so the run's
`repeat_frames` is also the frame count (`acquire.Engine.run(repeat_frames=...)`).

**MODE 3 (repeat + stream) is DSWave's own path, not something this driver introduced.** Two setters maintain
`bsp+0x35` (roll) in repeat mode, `FUN_140350ad0` (trigger mode) and `Mso_SetHoriTime 0x351200` (window change; it
returns at once in loop mode). Both apply the same rule: roll = auto_trig && window > hw+0x70 (200 ms):

```
    mov  %dl,0xb0(mso)                 ; auto_trig
    test %dl,%dl
    jne  auto
    movb $0,0x35(bsp)                  ; auto_trig = 0                -> roll = 0
    jmp  done
auto:
    cmpb $0,0x34(bsp)                  ; repeat
    je   done                          ; not repeat -> roll unchanged (Mso_ComputeGeometry sets it from the data rate)
    mov  0x78(mso),%r9                 ; sample_time_ps
    cmp  %r9,0x70(profile)             ; 200 ms in the M454 USB 3 profile
    setb %cl                           ; roll = sample_time > 200 ms
    mov  %cl,0x35(bsp)
done:
```

So repeat with the trigger off and a window above 200 ms ends in `HW_BASE_MODE 3` in DSWave too, and there
`transfer_count` follows the roll branch instead of the repeat 2.

**(hardware, 2026-11-16)** Mode 3 was measured: 100 kHz square, 0-2.8 V from the onboard generator into A0, trigger
off, 250 ms window, 1-channel and 4-channel configurations. The wire shows `W 0x00 = 0x0003` (repeat + stream), one
transfer per frame of `transfer_size = pkt_bytes + 4096` bytes (204800 for 1 channel at 800 kSa/s), the 4 KiB tail at `acqu_bytes`, and the
normal stop handshake. What it does **not** do is carry a frame the way mode 1 does:

| | mode 1 (100 ms window, 800 kSa/s) | mode 3 (250 ms window, 800 kSa/s) |
| --- | --- | --- |
| tail at `acqu_bytes` | yes | yes |
| `tail.valid_words` | 20064 = `acq_words`: the whole record | 112, 880, ... from frame to frame: a running counter |
| decoded with the tail | 80128 samples; 100.0 kHz, 2.89 Vpp | 448, 4576, ... samples (7, 55 blocks) |
| decoded without the tail | - | 100 kHz reads as 44.9 kHz (2.93 Vpp); the 4-channel setup reads 8.68 Vpp noise |

So in mode 3 the per-frame tail stops describing the record, and the block layout is not the one `geometry()`
derives: a raw transfer decoded as a single-channel record is not the signal.

**(hardware, 2026-11-16, the same setup) Mode 3 is a stream, not N acquisitions.** Five consecutive 204800-byte
transfers of one `--repeat 5` run are 99 % byte-identical to each other, while mode 1 frames of the same signal are
5 % identical (best alignment 1484 samples): in mode 3 every read returns a chunk of *the same* continuous buffer,
not a separate recording. The discontinuities inside a chunk (95-99 % of the sample intervals are exactly 8 at
800 kSa/s; at 8 kSa/s, same window, 99.3 % over 4096 samples) are where the buffer advanced between two reads, and
they scale with the data rate - a readout effect, not framing.

Consequence: the window limit applies to the **untriggered** form. A triggered repeat is not subject to it, and it
is the form DSWave itself records (`corpus4 repeat_mode`): the device re-arms for every frame, so with the trigger on
roll stays off and the mode is 1 at any window. From the corpus4 `repeat_mode` settings the driver's triggered repeat
program matches DSWave's recorded setup block write by write (75 writes without CROSS; HW_BASE_MODE 1, 2 transfers of
transfer_size 507904 = pkt_bytes 503808 + the 4 KiB tail), and every recorded frame's own tail gives the trigger
index DSWave's HAL logged
(`tests/golden/test_repeat_golden.py`, skipped without the local traces).

**(hardware, 2026-11-16)** `--repeat 3 --trig A0:rise:1.4V --trig-mode normal --duration 250ms`, 100 kHz square into
A0: three separate 250 ms records (200192 samples each at 800 kSa/s), 100.000 kHz and 2.89 Vpp in frame 0, 99.7 % of
the sample intervals exactly 8, the three frames 0 % byte-identical to each other, stop normal, 1.1 s. With a 2 Hz
source instead, three frames take 1.75 s - i.e. each frame waits for its own trigger.

So `--repeat` carries two behaviours, chosen by the trigger:

* **Triggered**: `--trig SRC:EDGE[:LEVEL] --trig-mode normal`. Every frame is its own acquisition and waits for its
  own trigger: the first up to `--timeout` (+ `--holdoff`); after it the frames share one deadline (below) that
  grows by `--timeout` + `--holdoff` per frame, so a later frame may wait longer than `--timeout` when earlier ones
  came quickly. `HW_BASE_MODE` stays 1 at any window. Repeat mode with a trigger
  has no auto mode (the device re-arms every frame, so a frame cannot fall back to an untriggered capture):
  `--repeat N` with `--trig` and `--trig-mode auto` (the CLI default) is refused with `INVALID_ARG` (exit 2) before
  the device is opened, with the hint to use `--trig-mode normal`, or `--trig-mode force` for untriggered frames.
  No trigger on the start is `TIMEOUT_NO_TRIGGER` (exit 5), with no forced fallback.
* **Untriggered**: no `--trig` (any trigger mode, including auto) or `--trig-mode force`. The frames follow the start
  back to back; the head wait is 2 s + record + 0.5 s (no head: `PROTOCOL_ERROR`, exit 6). An untriggered repeat
  whose applied window (the HAL sample time, not `--duration`) is above 200 ms would switch the device to
  `HW_BASE_MODE 3` (the stream above) and is refused with `INVALID_ARG`; the message names the applied window and
  says when the 1024-sample minimum of an explicit `--rate` lengthened `--duration` (`-c A0 --rate 5k --duration 100ms`
  applies 0.256 s at 4 kSa/s and is refused). `stream` and `log` are for that case.

The vendor datasheet (V1.0.0, 2025-11-01) draws the same line from the other side: **实时 (real-time)** recording
holds 4 Mpts and, at the maximum sample rate, 10 ms; **单次 (single)** holds 400 Mpts and 1 s. The product has two
trigger modes (Auto, Normal) and no repeat mode at all - `:REPEATModeEnable` is a HAL command, and it produces
separate records only while roll is off.

**What is measured.** The untriggered repeat program has no DSWave recording behind it (the only repeat scenario
recorded from DSWave, `corpus4 repeat_mode`, is the triggered one); it differs from DSWave's recorded forced single
capture of the same settings in one write, `HW_BASE_MODE` 1 instead of 0 (2 below the 102.4 MB/s stream
threshold). On hardware (2026-11-17, one unit, fw `0x3110`, Windows; D0 and
D1 on the 1 kHz calibration output, A0 on the generator): 2858 untriggered frames over 8 configurations - D0/D1 alone
at 10 MSa/s x 3 ms x 50, 100 MSa/s x 1.5 ms x 50, 400 MSa/s x 50 us x 500, 1 MSa/s x 150 ms x 5 and x 200 ms x 3 (the
largest window); A0 + D0/D1 at 10 MSa/s x 2 ms x 50 and 100 MSa/s x 200 us x 200 (10 kHz sine); A0 alone at
400 MSa/s x 20 us x 2000 (1 MHz sine). Every run returned all its frames with a normal stop and one stop handshake;
the frames are separate acquisitions (the calibration square's phase moves from frame to frame by a constant step,
1000 us period, 50 % duty, D0 = D1); every analog frame read the generator frequency within its resolution (10 kHz
+-0.3 %, 1 MHz +-0.015 %) and 2.08-2.09 Vpp; a normal capture after every run was correct. Triggered (the same
day): A0 rise at 1.5 V (1 kHz square, 10 MSa/s x 2 ms x 20), D0 rise and D0 fall (100 MSa/s x 200 us x 20 each) and
A0 at 1 MSa/s x 250 ms x 3: every frame had its edge at t = 0; a trigger that never came gave `TIMEOUT_NO_TRIGGER`,
auto mode was refused. Earlier: 50/500/2000 frames in one start, every frame with its own tail and trigger index,
stop normal (2026-11-13), and the 100 ms runs above.

**Repeat run limits and reporting.** Engine deadline after the head of a repeat run: max(10 s, 1.5 x record + 2 s,
min(3600 s, 1.5 x N x (record + 1 ms per-frame floor + transfer_size / 50 MB/s) + 2 s [+ (N-1) x (`--timeout` +
`--holdoff`) for a triggered start])); the 1 ms floor is about 10x the measured 38-108 us per frame, and
transfer_size (the driver's `geom['pkt_bytes']`) is the transfer with its 4 KiB tail. A run whose planned time (the
same sum without the 3600 s cap) exceeds 3600 s is refused offline with `INVALID_ARG` (it binds for long windows of
few samples, where the memory cap does not, and for triggered runs, whose trigger waits count too), so a run is never
cut at the cap. Memory cap: 1 GiB
for N x (transfer_size + decoded bytes), where decoded bytes = analog_depth x analog channels + digital_depth x
digital channels (1 byte per sample per channel, digital unpacked from 1 bit to 1 byte); D0-D7, 1 ms, 100k samples,
`--repeat 10000` is refused (8647 MiB, about 8.4 GiB). Every repeat run is followed by the traced 100 ms hold
(section 7, item 7), whatever `OPENM454_TIMING` says. With fewer than N frames collected,
a triggered start gives `TIMEOUT_NO_TRIGGER` (exit 5), an untriggered one `TIMEOUT_DATA` (exit 6), an abort
`TIMEOUT_DATA` "(aborted)"; none hints at USB 3 and the partial frames are not returned. A digital rise/fall trigger
in repeat mode is neither checked for misfires nor re-armed (`--trig-retries` has no effect): `acq.trigger_check` is
`unchecked`, `acq.trigger_check_reason` "repeat mode: frames are neither checked nor re-armed". `stream` and `log`
have no `--repeat` on the CLI, and through the API refuse `Settings.repeat != 0` with `INVALID_ARG`; the HAL repeat bit is set only when capture/plan
pass a count > 0. A negative `--repeat` is `INVALID_ARG` (exit 2, one JSON document), refused before the device is
opened; 0 is off.

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
confirmed against DSWave for the 73 scenarios of `tests/fixtures/scenarios/corpus5.json` (`tests/golden/test_awg_modes_golden.py`,
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
