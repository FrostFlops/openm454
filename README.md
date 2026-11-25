# openm454

English | [简体中文](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md)

[![CI](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml/badge.svg)](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml)

An open driver, command-line interface and MCP server for the **DreamSourceLab DSCope M454** USB mixed-signal
oscilloscope (4 analog channels up to 400 MSa/s, 8 digital channels, built-in 0-3 V signal generator), written so
that an AI agent (Claude Code, or any MCP client) can capture and analyze real signals by itself, without the vendor
GUI.

The M454 speaks a newer ("v3") USB protocol than the devices supported by the open-source DSView. The protocol was
recovered from DSWave 1.2.1 by static analysis, by driving DSWave's own hardware layer through injected commands and
by USB tracing; see `docs/PROTOCOL.md`.

## Use

Requirements: Windows, Python 3.10+, the scope bound to WinUSB (it is by default), a USB 3 port and cable (captures
refuse a USB 2 link with `UNSUPPORTED`), and DSWave 1.2.1, needed once to
extract the FPGA bitstream (it is never redistributed). Close DSWave before using the driver: the scope accepts one
USB client.

```
pip install openm454            # or, from a clone: pip install -e .
```

The openm454 data folder is the clone itself, or for an installed copy `%LOCALAPPDATA%\openm454`
(`~/.local/share/openm454` elsewhere; `OPENM454_HOME` overrides it). It holds the cached bitstream (`firmware/`),
state (`state/`) and captures (`captures/`).

### Where the FPGA bitstream comes from

DSWave 1.2.1 (`DSWave.exe`) is needed once. openm454 only reads its files, looks in these places in order (each may
be the folder holding `DSWave.exe` or the exe itself) and searches no disk beyond them:

1. the environment variable `OPENM454_DSWAVE` (`DSCOPE_DSWAVE` is accepted too);
2. the path saved in `state/dswave.json` of the data folder (written by `openm454 setup --dswave`, below);
3. `DSWave/` in the data folder (a copy of the DSWave folder);
4. the Windows uninstall entries of DSWave in the registry: a normal DSWave installation is found with no setup at all;
5. `%ProgramFiles%`, `%ProgramFiles(x86)%` and `%LOCALAPPDATA%\Programs`, each in `DSWave\` or
   `DreamSourceLab\DSWave\`.

The bitstream is extracted the first time the scope's FPGA has to be configured (the first command after the scope is
powered up) or by `setup --dswave`, checked by SHA-256 and cached in `firmware/` of the data folder; DSWave can then be
closed, moved or uninstalled. `setup` without `--dswave` only reports (`cached`, `dswave`, `source`, `ready`). If nothing is found, the error is `FIRMWARE_SOURCE` (exit 6)
with what was searched and what to do. To see the state, or to tell openm454 where DSWave is once (no scope needed):

```
python -m openm454 setup                          # cached? where is DSWave found, and how
python -m openm454 setup --dswave <folder>        # check DSWave 1.2.1 there, cache the bitstream, save the path
```

The saved path is read, with no restart, by every openm454 process that uses the same data folder (the MCP server
included: if it was registered with `-e OPENM454_HOME=<folder>`, run `setup` with the same `OPENM454_HOME`).

```
python -m openm454 info
python -m openm454 capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m openm454 watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m openm454 awg set --wave sine --freq 10k --low 0V --high 2V
python -m openm454 analyze captures/<file>.npz --window=-1ms:1ms
python -m openm454 capture -c D0,D1 --rate 20M --duration 2ms --trig D0:pulse>:200us --decode "i2c:scl=D0,sda=D1"
python -m openm454 selftest
```

## Recording modes and measured speeds

| Command | How it records | Length | Rates |
|---|---|---|---|
| `capture` | into the 512 MiB sample memory, read out afterwards (287 MB/s sustained, see the ceiling below) | one record: 400M samples per channel / 512 MiB | up to 400 MSa/s (1 channel) |
| `stream` | every record is sent while it is acquired, at any data rate (`--record-first`: above 102.4 MB/s acquire it first and read it out afterwards, as DSWave does); longer than one record or `--duration forever`: record after record, each refreshing the previous one | unlimited; between records ~0.2 s while the readout keeps up, plus the rest of the record above it (0.7 s at 250 MB/s; 2.4 s with `--record-first`); the last record is kept (`--save-each`: all) | as `capture`: analog and digital at their own rates |
| `log` | loop mode: one endless gap-free stream, kept in RAM (default; written once at the end; a RAM ring keeps the newest part of longer runs) or, with `--storage disk`, dumped in chunk files while running | until `openm454 stop` or the duration; RAM budget / disk allowance | one rate for all channels, 100 MHz / N for any integer N (1 kSa/s..100 MSa/s, e.g. 33.333 MSa/s); interleaved 200 MSa/s (<= 2 analog, <= 4 digital channels) and 400 MSa/s (<= 1 analog, <= 2 digital) |

### Readout ceiling (measured 2026-11-18)

One Linux host: kernel 7.3.4, xHCI, usbfs 1000 MiB, the M454 on a 5 Gbps root-hub port.

| what | measured |
| --- | --- |
| 1 analog at 400 MSa/s (400 MB/s produced) | **287 MB/s**; the planned 1.2 s window (480 MB) took 1.675 s of wall time to read and ended 194 MB behind |
| 2 analog at 200 MSa/s (400 MB/s produced) | 287 MB/s |
| 2 analog + 8 digital at 100 MSa/s (300 MB/s on the wire) | 287 MB/s |
| 4 analog + 8 digital at 50 MSa/s (250.8 MB/s on the wire) | **207 MB/s**, 67 MB of backlog in 1.2 s |
| read out after the record instead of streaming (1 analog at 400 MSa/s, 200 MB) | 283.6 MB/s |
| pacing at the ceiling | 1.05 MB every 3.68 ms (382 transfers in 1.396 s), 15 of the 16 queued reads always in flight |
| unchanged by | kernel 7.2.15 -> 7.3.4, xHCI bus 004 -> 002, usbfs 16 -> 1000 MiB, read size 256 KiB .. 2 MiB, queue depth 4 .. 16 |

A `log`/`record` duration is enforced on the **bytes read** (the planned window), not on the wall clock: when the
produced rate is above the readout rate the run keeps going until the host has read that window, so the ring ends up
holding `(D - R) x wall time` — 194 MB here, which is why the arithmetic `(400 - 287) x 1.2 s` does not reproduce it.

Two different numbers, and the driver reports the second: the device's own wire rate is
`rate x (analog channels + digital channels / 8)` bytes/s (measured against signal time: 200.1 MB/s for 4 analog at
50 MSa/s, 250.8 MB/s for 4 analog + 8 digital at 50 MSa/s, 50.0 MB/s for 8 digital at 50 MSa/s — the model holds to
0.5 %), while `host_mb_s` is what the host actually sustained. When a set produces more than the readout can move,
the difference waits in the sample memory; when a set is *below* the ceiling but still falls behind (the 4 analog +
8 digital row), the readout itself is configuration dependent — why twelve channels top out below ten is open.
The host was never short of work (15 of the 16 queued reads in flight at every completion), so the host-side stack
is the wrong place to look — that is not proof that the device is the limit, only that nothing above it is waiting.

`bench`'s readout probe is a short sample of the same thing: it reported 206 MB/s for the set that sustains
207 here (4 analog + 8 digital) and 267 MB/s for the one that sustains 287 (2 analog + 8 digital).

Planning does not need a static ceiling: when this channel set has been measured at **another sample rate** the
lowest *full* measurement is used (the readout follows the channel set far more than the rate). A run in which the
host merely kept up measures the rate it produced, not the readout, so it is treated as a floor: it decides whether
the host can keep up, and it can raise the 190 MB/s planning ceiling, but a plan is never refused on it. A set with
no measurement at all falls back to the ceiling. A run still probes the exact configuration before it starts; the
plan says `provisional` while it does.

What a channel configuration costs the signal itself is a separate question, measured the same way (300 records per
configuration in four sessions, rate-matched, paired differences with 95 % intervals): see
[docs/SIGNAL-QUALITY.md](docs/SIGNAL-QUALITY.md). Above two analog
channels the differences stay below one code (1 LSB = 1.7 mV at 0.05 V/div); enabling the eight digital channels
changes nothing measurable; the one-channel mode is a different acquisition path (1.0 % higher gain, 0.5 LSB
interleave mismatch, 0.5 LSB lower noise).

Earlier measurements (2026-11-01/02), kept as recorded — every input on a 1 kHz square, every period checked for gaps:

- The data rate is rate x (analog channels + digital channels / 8) bytes/s.
- `stream`: real time at DSWave's 100 MB/s ceiling and at 200 MB/s. At 400 MB/s the readout ran at 270-290 MB/s and
  the rest waited in the sample memory; the record arrived complete. 12 channels at 50 MSa/s (250 MB/s, ~2 s
  records): 0.74 / 0.66 s between records when sent while acquired (the default), 2.35 / 2.45 s with
  `--record-first`; records complete and continuous.
- `log`: 150 MB/s (2 analog + 8 digital at 50 MSa/s) for 3 min (27 GB) and for 30 s to disk with a constant host
  lag; 200 MB/s (2 analog at 100 MSa/s) for 20 s without backlog; 5 MB/s for 10 min. At 250 MB/s (4 analog + 8
  digital at 50 MSa/s) the readout reached ~200 MB/s and the backlog grew ~55 MB/s. A 300 MB/s burst of 3 s (2
  analog + 8 digital at 100 MSa/s) read out at 266 MB/s and stayed complete.
- `log` above 100 MSa/s interleaves the converters exactly as `capture` does (DSWave never does this in loop
  mode). Verified on the scope: 1 analog channel at 200 and 400 MSa/s, 2 analog at 200, 2 digital at 400, 4 digital
  at 200 and mixed sets. 2 digital channels at 400 MSa/s or 4 at 200 MSa/s are 100 MB/s and run without a time
  limit. 1 analog channel at 200 MSa/s (200 MB/s) ran 10 s without backlog.
- The sample memory is a 512 MiB ring in loop mode: the host may fall up to ~512 MiB behind without losing data (host
  stalls of 10 s at 50 MB/s were absorbed). Beyond that the scope overwrites unread data without any flag and keeps
  streaming: from then on the data are garbage (one phase step on analog, the digital channels misaligned) until
  the run is restarted. Measured with the driver's backlog estimate, the break came at 508-514 MiB (three runs), so
  `log` stops at 480 MiB.
- `log --plan` (no device needed) prints the verdict for a rate, the gap-free limit and recommendation (next
  section), how long the run can last (`max_duration_s`) and where the data goes: RAM by default (refused when it
  does not fit the budget unless `--keep-last`), `--storage disk` for ~256 MB chunk files (it warns about SSD wear:
  200 MB/s is 720 GB per hour), `--storage none` for statistics only.
- `bench` measures this PC (free RAM, decode speed, disk write speed, USB readout) and records how much RAM and disk
  the owner allows for recordings and the data folder (`bench --ram 8GB --disk 50GB --dir <folder>`,
  `state/host.json`); `log` takes its defaults from there. Measured on one host: decode 468 MB/s, USB readout 206 MB/s
  (4 analog + 8 digital) / 267 MB/s (2 analog + 8 digital).

### Repeat mode (`capture --repeat N`)

`capture --repeat N` collects N back-to-back frames in one start: the device re-arms between frames (register 0x00
`HW_BASE_MODE` = 1) instead of the host starting a run per capture, which costs ~193 ms of protocol spacing (about
5.2 captures per second).

- Two kinds of start, and they behave differently:
  - **triggered** (`--trig SRC:EDGE[:LEVEL] --trig-mode normal`): every frame is its own acquisition and waits for
    its own trigger, the first up to `--timeout` (+ `--holdoff`); after it the frames share one deadline that grows
    by `--timeout` + `--holdoff` per frame, so a later frame may wait longer when earlier ones came quickly.
    `HW_BASE_MODE` stays 1 at any window, and the register program is DSWave's recorded triggered repeat program
    write for write. Measured (2026-11-17, Windows): A0 rise at 1.5 V on a 1 kHz square (10 MSa/s, 2 ms, 20 frames),
    D0 rise and D0 fall on the 1 kHz calibration output (100 MSa/s, 200 us, 20 frames each) and A0 with a 250 ms
    window (3 frames): every frame had its edge at t = 0. Earlier (2026-11-16, 100 kHz square, `--duration 250ms
    --repeat 3`): three separate records, 0 % byte-identical, 1.1 s; with a 2 Hz source the same run takes 1.75 s. `--trig-mode auto` (the CLI default) with `--repeat` and `--trig` is refused with
    `INVALID_ARG`: repeat mode with a trigger has no auto mode (use `--trig-mode normal`, or `--trig-mode force` for
    untriggered frames). No trigger on the start is `TIMEOUT_NO_TRIGGER` (exit 5), with no forced fallback.
  - **untriggered** (no `--trig`, any trigger mode including auto, or `--trig-mode force`): the frames follow the
    start back to back. An applied window (the HAL sample time, not `--duration`) above 200 ms would switch the
    device to `HW_BASE_MODE 3`, a stream and not N acquisitions, and is refused with `INVALID_ARG`; the message
    names the applied window and says when the 1024-sample minimum of an explicit `--rate` lengthened `--duration`
    (`-c A0 --rate 5k --duration 100ms` applies 0.256 s at 4 kSa/s). Raise or drop `--rate`, keep the window <= 0.2 s,
    trigger the frames, or use `stream`. This program has no DSWave recording (DSWave's own repeat scenario is the
    triggered one); it differs from DSWave's recorded forced capture of the same settings in one write, `HW_BASE_MODE` 1 instead
    of 0 (2 below the 102.4 MB/s stream threshold).
    Validated on hardware (2026-11-17, Windows): 2858 frames over 8 configurations, D0/D1 on the 1 kHz calibration
    output at 1-400 MSa/s with windows from 50 us to 200 ms, and A0 on the generator (10 kHz, 1 MHz): every run
    complete with a normal stop, every frame a separate acquisition (the square's phase moves from frame to frame),
    every analog frame at the generator frequency, and a normal capture after every run correct.
- Limits: the run's deadline is max(10 s, 1.5 x record + 2 s, min(3600 s, ...)) with a per-frame cost of the record
  plus 1 ms plus the transfer at 50 MB/s, plus (N-1) x (`--timeout` + `--holdoff`) for a triggered start; a run whose
  planned time, 1.5 x N x (record + 1 ms + transfer) + 2 s plus those trigger waits, exceeds 3600 s is refused with
  `INVALID_ARG` before the device is opened (lower `--repeat` or `--timeout`, shorten the window, or use `log` /
  `stream`). The host memory cap is 1 GiB for N x (the transfer with its 4 KiB tail + the decoded bytes, 1 byte per
  sample per channel, digital unpacked from 1 bit to 1 byte): D0-D7, 1 ms, 100k samples, `--repeat 10000` is refused
  (8647 MiB, about 8.4 GiB). A negative `--repeat` is `INVALID_ARG` (exit 2), refused before the device is opened; 0
  is off. Every repeat run is followed by the traced 100 ms hold whatever `OPENM454_TIMING` says (the device re-arms
  after every frame). `stream` and `log` have no `--repeat`; through the Python API they refuse
  `Settings.repeat` with `INVALID_ARG`. The MCP `capture` tool has no repeat parameter.
- Fewer frames than N: triggered gives `TIMEOUT_NO_TRIGGER` (exit 5, "collected k of N frames"), untriggered
  `TIMEOUT_DATA` (exit 6), an abort `TIMEOUT_DATA` "(aborted)"; the partial frames are not returned. A digital
  rise/fall trigger is neither checked for misfires nor re-armed (`--trig-retries` has no effect):
  `acq.trigger_check` is `unchecked` with `acq.trigger_check_reason`.
- Each frame is decoded on its own. The JSON has `acq.repeat_frames` (asked) and `acq.frames` (collected) and, with
  more than one frame, after `files`/`hints`/`warnings`, `frames_summary` over every frame (`count`, `listed`, `without_trig_index`,
  `without_trig_index_first`, `host_s`, and `analog` per channel with `vpp_v` / `vmin_v` / `vmax_v` as
  `{min, median, max}`) and `frames`, the first 100 frames only, each with `index`, `trig_index`, `host_s` and
  `analog` basics. `analyze` of a repeat `.npz` reports the same. `Capture.frames` holds one dict per frame
  (analog, digital, `trig_index`, `host_s`).
- `host_s` is when the frame's transfer completed on the host: seconds since the start command (the `CTRL` write,
  so the start settle is included), monotonic clock, stamped in the libusb completion callback — USB transport is
  included, and the event loop is not. `queue_us` is the other half: how much later the loop picked that transfer up,
  i.e. the host-side queue delay. `frames_summary.host_s` carries `first`, `last`, `interval_ms` (`min` / `median` /
  `max` between consecutive completions) and `queue_us` (`min` / `median` / `max`). Together they separate the two
  ways a frame can look odd: the device sampled a stale value (content wrong, completion on time) and the host was
  late (a long completion interval, or a large queue delay with several transfers drained in one event-loop pass).
  Both are host measurements, not device ones: the frame's own `trig_index` and content stay the authority for what
  the device acquired, and neither stamp is the device's sample or trigger time.
- The `.npz` keeps every frame (with more than one; `--repeat 1` saves a plain single capture): `analog_A1` / `digital_D0` hold frame 0 as for a single capture, `frames_analog_A1`
  is (frames, samples) uint8 codes, `frames_digital_D0` is (frames, ceil(samples/8)) uint8, each row packed with
  `np.packbits(bitorder='little')` (not frames x samples), plus meta `frames_samples`, `frame_trig_index` and
  `frame_host_s` and `frame_queue_us` (one entry per frame, absent when the frames were replayed from a fixture);
  `load_capture` unpacks them and restores `host_s` / `queue_us` on every frame. `--measure`, `--edges`, `--dump`, `--decode`, the JSON analog/digital blocks and the
  PNG describe frame 0 only. Bytes a frame carries beyond its record are device buffer content, not signal, and are
  cut by the frame's tail.
- Measured on one Linux unit over USB 3 (2026-11-16, 1 analog channel, default queue depth): 5 us window at
  400 MSa/s, 2048 samples per frame, 2000 frames in 452 ms (380 ms of that is setup) = 4.4 kframes/s end to end,
  ~38 us per frame after the setup; 10 us / 4096 samples: 2000 frames in 478 ms. On Windows (2026-11-17, whole
  call): A0 at 400 MSa/s, 20 us (8192 samples), 2000 frames in 0.29 s; D0/D1 at 400 MSa/s, 50 us, 500 frames in
  0.29 s. Repeated single captures cost ~193 ms of protocol spacing each (5.2/s).

### Above the readout limit: how `log` decides

D = the data rate; R = how fast this PC reads the scope while it acquires. R depends on the channel set (measured
on one host: 202 MB/s for 4 analog + 8 digital at 50 MSa/s, 264 MB/s for the same at 100 MSa/s and for 2 analog + 8
digital at 100 MSa/s, 275 MB/s for 4 analog at 100 MSa/s).

1. R is measured, not assumed: the first `log` above 150 MB/s with a channel set runs a probe of up to 2 s (a
   `probe` line) and remembers R in `state/host.json`: the measured rate, or "kept up" (R >= D) when it reads at
   >= 99 % of D. Later runs above 150 MB/s refresh it when the result is clear (still falling behind at a steady
   rate at the stop, or caught up at the end), and so do `bench` and `--remeasure`. Until then `--plan` assumes
   190 MB/s and marks the plan `provisional`.
2. D <= R: the host keeps up (`sustained` / `near_limit`); the run lasts as long as the storage allows.
3. D > R: the scope gets D - R ahead per second (`backlog_growth_mb_s`), and it sends nothing after a stop, so the
   unread part is lost. A log is gap-free for at most `gapfree_max_s` = 480 MiB x R / (D x (D - R)), the signal the
   host has read when the scope is 480 MiB ahead. A shorter log runs normally.
4. A longer log (or `forever`) is compared with what `stream` would do with the same channels and settings
   (`stream_alternative`, from stream's own planning): records of up to 400M samples per channel / 512 MiB, sent
   while acquired, with gaps of record x (D - R) / R + 0.18 s, at stream's rates (1-2-5 steps x interleave; it may
   run a domain slower, e.g. 2 analog + 8 digital at 100 MSa/s samples the digital channels at 50 MSa/s):
   - `STREAM_BETTER` (exit 2): stream samples every channel at least as fast, and its records are at least as long
     as the log's one gap-free piece (or one record holds the whole duration). The error carries the stream
     command, with the same vertical settings.
   - `NEEDS_FORCE` (exit 2): the log's piece is the longest gap-free recording possible but shorter than asked.
     `--force` records it and then stops (`stop_reason` `ring_full`, ok, with a warning); the rest of the requested
     time is not recorded. Both errors carry the numbers in `error.plan`.
5. While running, every statistics line has `backlog_mb`; while the backlog grows, also `backlog_growth_mb_s`,
   `ring_full_in_s` and `gapfree_projected_s`. The result has `unread_at_stop_mb` (acquired, never read).

Examples: 12 channels at 50 MSa/s (250 MB/s, R = 204): gap-free 8.88 s vs stream records of 2.03 s with 0.64 s
gaps, so `NEEDS_FORCE`; forced, the run stopped after 8.84 s (8.30 s with R = 202 in an earlier run), every period
present up to the last sample. 12 channels at 100 MSa/s (500 MB/s, R = 264): 1.13 s vs 1.02 s records with 1.09 s
gaps, still `NEEDS_FORCE`; with a readout below ~250 MB/s `stream` would win. 200 MB/s read at 190 MB/s: 10 MB/s
backlog growth, the guard after ~50 s, 47.8 s of gap-free signal.

The backlog is estimated as D x (time since the head) - bytes received, checked on every USB transfer, against a
baseline that follows the scope / PC clock difference: it may rise by at most 200 ppm x D per second, and only
while the host is caught up (estimate below 16 MiB), so the estimate stays exact over hours and a real backlog is
never absorbed. A guard stop while nothing arrives at all is reported as a stalled scope (`TIMEOUT_DATA`); a
forced log that was planned to end at the guard reports `ring_full`, any other guard stop `HOST_TOO_SLOW`.

### `log` or `stream`?

Both record untriggered, but they are not the same mode of the scope. `log` is one endless run (loop mode);
`stream` is a series of records, each programmed with a fixed length that the scope stops at.

| | `log` | `stream` |
|---|---|---|
| gaps | none inside a run | one per record (refresh) |
| gap between two runs, 12 channels at 20 MSa/s (100 MB/s) | 0.18 s | 0.19 s |
| gap between two runs, 12 channels at 50 MSa/s (250 MB/s, above the readout limit) | 0.51 s after a 1.5 s run: 0.33 s to read the run's backlog (data the scope acquires meanwhile are dropped), then 0.18 s restart | 0.66-0.74 s after a ~2 s record: the rest of the record is read (sent while acquired), then the restart; 2.33-2.45 s with `--record-first` (the whole record read out after it was acquired) |
| rates | one rate for every channel: 100 MHz / N for any integer N, interleaved 200 / 400 MSa/s on 1-2 channels | analog and digital at their own rates (e.g. 1 analog at 400 MSa/s with 8 digital at 100 MSa/s), from DSWave's 1-2-5 x interleave table (e.g. 80 MSa/s) |
| above the readout limit (~200-275 MB/s) | one gap-free piece of at most 480 MiB x R / (D x (D - R)) (the scope sends nothing after a stop; see above) | records of up to 512 MiB / D (400M samples per channel), each sent while acquired, with gaps |
| below ~51 kB/s (e.g. 1 channel at 1-20 kSa/s) | supported | the rate is raised |
| protocol decoders | offline on the saved file (`analyze --decode`) | `--decode` on the kept record |
| register program | DSWave's Data Acquisition mode (interleaving: this driver's extension, verified) | DSWave's own program for this window (golden-tested) |

The restart (~0.18 s) is the same for both; the difference is what has to be read out before it. Use `log` for
continuous recording; `stream` for different analog and digital rates, decoding the record, or above the readout
limit when `log` says `STREAM_BETTER` or the whole time matters more than one long gap-free piece.

```
python -m openm454 bench --ram 8GB --disk 50GB --dir logs
python -m openm454 log --plan -c A0,D0-D7 --rate 1M --duration 10min
python -m openm454 log -c A0,D0-D7 --rate 1M --duration 10min                   # kept in RAM, one .npz at the end
python -m openm454 log -c A0,D0-D7 --rate 1M --duration forever                 # RAM ring; end it with: python -m openm454 stop
python -m openm454 stream -c D0-D7 --rate 100M --duration 60s --every 1s
```

Every command prints one JSON document (`watch`, `stream` and `log`: one per line, then the result), whether or
not `--json` is given: measurements, applied settings, file paths. Samples are never printed (unless `--dump N` asks
for a decimated series); captures are saved as `.npz` (raw codes + scaling) and `.png`. In the plot the analog legend is one row above the plot area (one entry per
channel, e.g. `A1  1V/div  Vpp 2.86V  f 1kHz`) with the title above it, digital lane labels sit in the right margin,
and only the trigger markers (dashed line at t = 0, dotted analog trigger level) are drawn over the waveform; decoded
frames are not drawn, they are in the JSON and `<capture>_decode.json`. `python -m openm454 <command>
--help` lists every option. Python API: `openm454.scope.Scope`, `openm454.capture.load_capture`, `Capture.decode()`, `openm454.plot.render(cap, window=None, title=None, summary=None,
channels=None)` (returns the figure; the caller closes it), `openm454.firmware.status()` / `save_dswave()`.

Protocol decoders (`--decode SPEC`, repeatable, on `capture`, `watch`, `stream` and `analyze`):
`uart:rx=D4,baud=115200|auto,bits=8,parity=none,stop=1,invert=0`, `spi:clk=..,mosi=..,miso=..,cs=..,mode=0..3`,
`i2c:scl=..,sda=..`, `can:rx=..,bitrate=500k|auto,invert=0`. They report every frame with trigger-relative times and
its errors (parity/framing, partial words, NACKs, CRC/stuff/form errors), and remove the comparator chatter of slow
edges. Output format: `docs/schemas/decode.json`. Decoding needs about 10 samples per bit or clock (I2C: 9 per SCL
period, no errors; 5-6 per period still decode with well under 1 % errors). They have been checked against real buses (SPI up to 18 MHz, RS232, CAN up to 1 Mbit/s, I2C up to 400 kHz)
generated by a microcontroller, including deep captures and decoded streams.

### Capture spacing (`OPENM454_TIMING`)

Every capture pays the spacing DSWave uses: 100 ms before the run, 50 ms between submitting the reads and the start
writes, two 15 ms settles and 15 ms per register read - about 193 ms per capture, a little over 5 captures per second.
A profile shortens selected waits for one session; the traced spacing stays the default and nothing changes without
the variable. The floor profile measured 35 ms per capture (p50) on one Windows bench:

```bash
OPENM454_TIMING=gap=0.005,quiet=0.005,setup=0.002,submit=0.005   # 35 ms per capture instead of 193 ms
```

Five keys (traced value, floor): `gap` 100 / 5 ms (before every run), `quiet` 100 / 5 ms (no register access after
the stop of a forced or triggered single run), `submit` 50 / 5 ms, `setup` 15 / 2 ms and `final` 1 / 1 ms (not
shortened: it is the stop handshake itself). A profile only shortens. Not adjustable, whatever the profile:

- `start` (15 ms after the start write) and `read` (15 ms per register read): shortened to 2 ms, `start` alone gave
  a capture taken with the settings of the capture before (1 of 300) and `read` alone ended the session in a STALL of
  the data transfer at capture 70. `start=...` and `read=...` are `INVALID_ARG`.
- The analog front-end settling (150 ms before the first analog run after a digital-only run or at session start,
  300 ms after a saturated input) is device physics: `wake=...` and `overdrive=...` are `INVALID_ARG`.
- After the stop of a run that ended without a head (a normal/auto timeout with no trigger, or a stop request before
  the trigger) and after every repeat run, the driver holds every register access for the traced 100 ms: writes sent
  within ~10 ms of such a stop were dropped on hardware.

Why these floors: a shortened wait can make the scope miss register writes with no error at all - the capture then
runs with the registers of the capture before while the host scales it with the new settings. On the owner's bench
(2026-11-17), forced captures alternating two settings on a 100 kHz square, the floors first proposed (the five above
plus `start` and `read` at 2 ms) gave 14 and 17 wrong readings in 300 and then a failed session (`DEVICE_ERROR: EP
0x84 handshake: expected 02, got timeout`); the floor profile above gave none in 3000 forced and 300 triggered
captures, nor after normal-mode timeouts followed at once by a new AWG program and V/div, auto-mode fallbacks, or
repeat runs. Zeroing `final` wedged the scope within 2-3 captures on a second bench (then it only answered again
after a replug). The full table: `docs/PROTOCOL.md` section 7.1.

An entry that is not `key=value`, an unknown or non-adjustable key, a key given twice, a value that is not a finite
non-negative number, or a value above the traced one or below its floor is `INVALID_ARG` (exit 2) before anything is
sent or the device is opened, for every command that can reach the device (and `log --plan`, `bench --no-device`,
`debug plan`); the MCP tools report it too. Every `Scope()` reads the variable again. While a profile is active it is
reported by `info` (`timing`), every capture's and stream's `acq.timing`, the `log` result document (the last line
`log` prints) and every saved log file (`acq.timing`), `watch` (`timing` in its first and summary line), the selftest
result, and the MCP summaries. `log --plan` applies it: the restart part of the seam between stream records
(`gap_s` = the drain, record x (D - R) / R, plus the restart) is 0.18 s traced and 0.027 s at the floor profile.

## CLI reference

`pip install openm454` installs the `openm454` command (`python -m openm454 ...` is the same) and the `openm454-mcp`
server. Global options, before the subcommand: `--max-wall T` (watchdog: stop and release the device after T or the
budget computed from the command, whichever is longer; it never shortens the computed budget), `--verbose` (wire
log on stderr), `--json` (accepted, output is always JSON).
`python -m openm454 <command> --help` lists every option.

| Command | Purpose | Main options |
|---|---|---|
| `info` | device, firmware, calibration, limits | |
| `capture` | one triggered (or untriggered) record: measurements, `.png`, `.npz` | see below |
| `watch` | repeated captures, one JSON line each (monitoring) | the `capture` options, `--every 0.5s` (start to start), `--count N` (default 20 when neither `--count` nor `--for` is given), `--for T`, `--fields A1.freq_hz,...`, `--stop-when EXPR`, `--save-last` (see [watch](https://github.com/FrostFlops/openm454#watch)) |
| `stream` | untriggered recording sent while it is acquired, live statistics | the `capture` options, `--every 1s`, `--save-each`, `--record-first` |
| `log` | gap-free endless recording (loop mode), one rate for all channels | `-c`, `--rate 1M`, `--duration 10s\|forever`, `--every 1s`, `--storage ram\|disk\|none` (`--no-save` = none), `--keep-last`, `--chunk`, `--ram`, `--plan`, `--force`, `--remeasure`, `--out`, `--tag`, `--awg`, `--cal`, `--hook FILE.py` (live processing, see [Live processing hook](https://github.com/FrostFlops/openm454#live-processing-hook-log---hook)), the vertical options |
| `bench` | this PC's recording limits (free RAM, decode and disk speed, USB readout); stores the RAM / disk allowance | `--ram 8GB`, `--disk 50GB`, `--dir`, `--disk-test-mb`, `--no-device` |
| `awg set` | continuous waveform on the MCX output (0..3 V) | `--wave sine\|square\|triangle\|noise`, `--freq 1k`, `--low/--high` or `--vpp/--dc`, `--duty 50%`, `--phase`, `--cal` |
| `awg sweep` | frequency sweep, repeating | `--start`, `--end` (required), `--time 100ms`, `--log`, `--wave`, `--high`, `--low`, `--duty`, `--cal` |
| `awg burst` | N periods of the carrier every period | `--cycles`, `--period 10ms`, `--freq`, `--wave`, `--high`, `--low`, `--duty`, `--cal` |
| `awg mod` | AM / FM / FSK of the carrier | `--kind am\|fm\|fsk`, `--mod-wave`, `--mod-freq`, `--depth`, `--deviation`, `--hop`, `--freq`, `--wave`, `--high`, `--low`, `--duty`, `--cal` |
| `awg dc` | steady DC level (0..3 V, 6.4 mV steps) | `--level` (required), `--cal` |
| `awg off`, `awg status` | park the output at 0 V / show the last setting | |
| `plot` | render a saved `.npz` to PNG (no device) | `file`, `--window`, `--channels`, `--out` |
| `analyze` | measure a saved `.npz` (no device) | `file`, `--window`, `--channels`, `--edges N`, `--dump N`, `--measure`, `--decode SPEC` |
| `cal` | opt-in PC-side calibration file ([Calibration](https://github.com/FrostFlops/openm454#calibration-opt-in)): `status` (default, no USB), `template`, `import` | `status`, `template --out FILE [--force]`, `import FILE [--bind]` |
| `selftest` | verify the scope ([Self-test](https://github.com/FrostFlops/openm454#self-test)) | `--quick`, `--no-awg`, `--fixture file\|standard` |
| `setup` | where the FPGA bitstream comes from; `--dswave <folder>` records DSWave's folder once (no USB) | `--dswave` |
| `release` | make sure the device is idle and released | |
| `recover` | reset and reload the FPGA of a hung scope, like a power-up (a scope that does not answer register reads still needs a replug) | |
| `stop` | ask a running `watch` / `stream` / `log` (e.g. in the background) to stop cleanly | |
| `debug` | maintenance: `regs-read`, `regs-write` (same safety policy), `plan` (print the register program for capture options without touching the device) | |

`capture`, `watch` and `stream` share these options:

- channels / vertical: `-c A0-A3,D0-D7` (also `analog`, `digital`), `--vdiv 1V`, `--offset 0V`, `--coupling dc|ac`,
  `--bw20 A1,A2`, `--invert A2`, `--probe 10x|A2=10x`, `--threshold 1.4V`, `--cal` (apply the calibration file, see
  [Calibration](https://github.com/FrostFlops/openm454#calibration-opt-in); off by default). Per-channel values are written
  `A0=500mV,A2=2V`.
- timing: `--timebase` (window = 10 divisions), `--duration`, `--samples`, `--rate`.
- trigger: `--trig SRC:EDGE[:LEVEL]` (e.g. `A1:rise:1.4V`, `D0:fall`, `D0:pulse>:200us`), `--trig-mode
  auto|normal|force` (default `auto`), `--trig-pos 50%`, `--auto-after 0.3s`, `--timeout 5s` (normal-mode wait),
  `--holdoff`, `--sensitivity 0..15` (default 5), `--trig-retries N` (default 4; digital edge triggers, see
  [Digital edge triggers](https://github.com/FrostFlops/openm454#digital-edge-triggers)). `stream` accepts the trigger options and ignores them: it always
  records untriggered.
- output: `--measure basic|full|none` (see [Measurements](https://github.com/FrostFlops/openm454#measurements---measure)), `--edges N`, `--dump N`, `--window t0:t1`, `--no-plot`, `--no-save`, `--out`,
  `--tag`, `--awg 'sine,10k,0V,2V[,duty%]'` (set the generator first), `--decode SPEC` (repeatable),
  `--decode-limit` (default 50 frames).

`log` takes the vertical options too; its timing is `--rate` and `--duration` (see the recording sections above).
A value that starts with `-` needs the `=` form on Python 3.10-3.12 (`--window=-1ms:1ms`, `--offset=-1.5V`; per
channel `A1=-1.5V` is fine as it is); the MCP server does this itself. `--trig` without a level on an analog channel
uses that channel's `--offset` as the level. `--rate` snaps down to a rate the scope has (at most 400M on 1 analog
channel, 200M on 2, 100M on 3-4; digital only: 400M on up to 2 channels, 200M on up to 4, 100M on 5-8).
`capture` keeps only the newest 200 captures with its `--tag` (default `cap`) in its output folder (`captures/` by
default, or `--out`): older files named the way openm454 names them (`<yyyymmdd-hhmmss-mmm>_<tag>.npz`, `.png`,
`_decode.json`) with that tag are deleted. Recordings with other tags (`log`, `stream`, `watch_last`) and any other file
in the folder are never touched.

Exit codes: 0 ok, 1 other (a failed self-test, an internal error, interrupted), 2 bad arguments, 3 scope not found,
4 busy, 5 no trigger, 6 device / protocol / watchdog failure, 7 refused by the safety rules (see Errors).

### watch

`watch` repeats `capture` and prints one JSON line per capture.

- `--every T` (default 0.5s) is the time from the start of one capture to the start of the next. If a capture takes
  longer, the next one starts at once, with no catch-up burst.
- `t_s` of a line is when that capture started, in seconds since the watch began (`--every 0.5s` gives 0, 0.5, 1.0,
  ...). The final line has `elapsed_s`.
- `--count N` and `--for T` end the watch; with both, whichever comes first. No capture starts at or after `--for`:
  `--every 0.5s --for 2s` gives 4 captures. With neither, 20 captures. `openm454 stop` ends the watch after the current
  capture, or within 0.25 s while it waits between captures.
- `--fields CH.KEY,...`: `KEY` is any measurement name; only the named channels are measured. The default fields
  are `vpp_v`, `vmean_v`, `freq_hz`, `duty_pct` and `state` of every channel.
- `--stop-when EXPR`: a Python expression with one dict per channel, e.g. `"A1['vpp_v'] < 1"`; an expression that
  raises counts as false. `--fields` or `--stop-when` together with `--measure none` is refused (exit 2).
- `--window` and `--edges` apply; `--dump`, `--no-plot`, `--no-save` and `--tag` have no effect. `--out` is only
  used by `--save-last`, which writes `<out>/<date>-<time>_watch_last.npz`.
- A line adds `trigger_check` when it is not `ok` / `unchecked`, and `trigger_misfires` when above 0.

### Measurements (`--measure`)

`--measure basic|full|none` applies to `capture`, `watch`, the final result of `stream` and `analyze`.

- `basic` (default). Analog channels: `samples`, `vmin_v`, `vmax_v`, `vpp_v`, `vmean_v`, `vrms_v`, `vac_rms_v`,
  `vtop_v`, `vbase_v`, `vamp_v`, `freq_hz`, `period_s`, `duty_pct`, `edges_rise`, `edges_fall`, `periods_in_window`,
  `pwidth_s`, `nwidth_s`, `rise_s`, `fall_s`, `overshoot_pct`, `undershoot_pct`, `clip_lo`, `clip_hi`, and when they
  occur `freq_method`, `freq_res_hz`, `period_irregular`, `noise_like`. Digital channels: `samples`, `state`, `high_frac`, `edges`,
  `freq_hz`, `period_s`, `duty_pct`, and when they occur `glitch_edges` and `runts`. On analog channels, frequency and duty come
  from falling edges when the window holds at least two falls but fewer than two rises (digital channels need two
  rising edges).
- On analog channels `freq_hz` comes from a least-squares line through all rising edges in the window (falling edges in the fall-to-fall case above); the median edge interval when glitches make the intervals irregular (`period_irregular: true`); the FFT peak when there are fewer than 2 rising and fewer than 2 falling edges or the crossings are noise-like (`freq_method: fft`). Digital channels use the mean interval from the first to the last rising edge (the median with glitches) and give no `freq_res_hz`.
  `freq_res_hz` = `freq_hz` / (edge span in samples) is the resolution bound: an edge faster than the sample
  interval is placed to about +-0.5 sample only. It is given on the fitted path only. A record that is short at a low
  rate still bounds the accuracy. Example: 9.9 kHz at 0.8 MSa/s over 5 ms spans about 3960 samples, so `freq_res_hz` is about 2.5 Hz (250 ppm); the fit over all ~49 rising edges usually lands well inside that bound.
- `full`: `basic` plus, per channel, statistics over every whole cycle (rising edge to rising edge) in the window:
  `cycles`, `period_min_s`, `period_max_s`, `period_std_s`, `freq_min_hz`, `freq_max_hz`, `pwidth_min_s`,
  `pwidth_max_s`, `nwidth_min_s`, `nwidth_max_s`, `duty_min_pct`, `duty_max_pct` (digital channels also get the mean
  widths `pwidth_s`, `nwidth_s`), and `cycles_capped: true` when the 100k-edge limit was reached (the statistics then
  cover the first 100k edges). Not given for channels with fewer than 2 rising edges or for noise. Analog channels
  also get `range_lo_v` and `range_hi_v`, the voltages the screen can show at the applied V/div and offset; readings
  at the ends are clipped. Edge lists (`edges_rise_s`, `edges_fall_s`) appear only with `--edges N`. Deep records
  take a second measuring pass.
- `none`: no `analog` / `digital` blocks (hints are still given). The MCP `detail` argument (`summary` | `full`) is
  a different thing: it chooses a compact summary or the CLI's whole document.

### Digital edge triggers

The digital inputs have no hysteresis. A slow edge (for example the ~8 us rising edge of the calibration square)
chatters at the threshold for tens of ns, so a falling-edge trigger can fire inside the rising edge, and the reverse,
while the hardware still reports a valid trigger. After every `capture` or `watch` triggered on a digital channel with
a rise or fall edge (normal or auto mode), openm454 therefore checks the settled level of the trigger line just
before and after t = 0 (a level is settled when it lasts at least 200 ns and at least 3 samples, whichever is longer;
shorter runs are chatter). It only calls a capture a misfire when the unsettled stretch between the two settled levels
is as short as chatter (at most 200 ns; measured chatter lasts up to about 120 ns); a longer stretch is real data and
gives `inconclusive`. So fast data right at the trigger (faster than about 10 Mbit/s, e.g. a short 1-0 pair of bits
between a long low and a long high) can be taken for chatter and re-armed, and an edge so slow that its chatter runs
last 200 ns or more passes as `ok`.
A capture that fired on the opposite edge is discarded and the scope re-armed, up to `--trig-retries N` times (default
4; 0 = only check; MCP `trig_retries`). Re-arms share the original trigger wait (`--timeout` in normal mode,
`--auto-after` in auto mode, plus `--holdoff`); only time spent waiting for a trigger counts. Analog triggers, `any`
and pulse triggers, `--trig-mode force` and the untriggered fallback of auto mode are not checked.

The capture's `acq` block reports it:

- `trigger_check`: `ok` (the first capture shows the requested edge); `misfire_retried` (misfires were discarded, the
  returned capture is correct); `mismatch` (the returned capture is itself a misfire: t = 0 is NOT the requested edge,
  with a warning and a hint); `inconclusive` (the edge could not be confirmed, e.g. no pre-trigger samples at
  `--trig-pos 0`, or a glitch or clock faster than 200 ns; never re-armed); `unchecked` (not a digital rise/fall
  trigger, or untriggered). MCP summaries leave out `unchecked`.
- `trigger_misfires`: the number of misfired captures discarded and re-armed (left out of MCP summaries when 0).
- `attempts` lists every run: `triggered`, `forced` (untriggered), `misfire` (fired on the wrong edge), `no-trigger`.
  `acq.triggered` keeps its meaning: the hardware trigger flag of the returned capture.
- Warnings and hints: `mismatch` gives a warning and a hint; `misfire_retried` also adds a hint with the number of
  discarded misfires (e.g. "D5 falling-edge trigger: N misfire(s) inside a rising edge were discarded and re-armed").

Measured on the M454 (1 kHz calibration square, normal mode): before the check, a D5 falling trigger fired inside
the rising edge in 13 of 40 captures at 100 MSa/s, D0 falling in 5 of 40, D5 rising never. With the check, 60 captures
each: no wrong edge returned at 50, 80 or 100 MSa/s (D5 alone, D5 with A1, all 8 digital channels; 5-25 misfires
re-armed per set). At 400 MSa/s on D5 alone about half of all tries misfire, and 2 of 60 captures misfired on all
5 tries: both were returned as `mismatch` with the warning, not as a success.
For a reliable digital trigger use the edge that is fast on the signal (a falling trigger misfires on a slow rise),
or trigger on an analog channel, whose trigger has a noise margin.

### Vertical limits

- `--offset` is the voltage at the centre of the screen. The allowed range is about -4.32 .. +4.36 divisions
  (-127 .. +128 offset codes), where a division is the hardware V/div (the requested value rounded up to a 1-2-5
  step, 1 mV .. 5 V at the probe input). At 1x:

  | V/div | allowed offset | V/div | allowed offset |
  |---|---|---|---|
  | 1 mV | -4.323 .. +4.357 mV | 100 mV | -0.4323 .. +0.4357 V |
  | 2 mV | -8.647 .. +8.715 mV | 200 mV | -0.8647 .. +0.8715 V |
  | 5 mV | -21.62 .. +21.79 mV | 500 mV | -2.162 .. +2.179 V |
  | 10 mV | -43.23 .. +43.57 mV | 1 V | -4.323 .. +4.357 V |
  | 20 mV | -86.47 .. +87.15 mV | 2 V | -8.647 .. +8.715 V |
  | 50 mV | -216.2 .. +217.9 mV | 5 V | -21.62 .. +21.79 V |

  Every number scales with the probe factor (at 10x: 10 mV .. 50 V/div at the tip, limits x10).
- A larger offset is clamped with a warning, e.g. `A1: offset 2.5 V out of range at 0.5 V/div (allowed -2.162 ..
  2.179 V, about +-4.3 div); clamped to 2.179 V`; the value used is in `applied.offset_v`. `watch` lines show no
  warnings.
- The trigger level must be on the screen (codes 10..245 around the offset), otherwise `TRIGGER_OUT_OF_RANGE`
  (normal and auto mode; with `--trig-mode force` an off-screen level is clamped without an error). Without a level,
  an analog trigger uses `--offset`.
- Clipping gives a hint (`A1 clips at the top: increase --vdiv or lower --offset`); the front end needs ~0.2-0.3 s to
  recover after an overdrive.

## Calibration (opt-in)

An optional, PC-side correction of the measured volts. It is off by default and never written to the device: the
device EEPROM is untouched and the scope's own zero calibration still applies; the corrections act on top of it, on
the PC. Without `--cal` nothing changes (no new keys, the same register program).

### The file and the commands

- The file is `state/calibration.json` in the data folder (the clone, or for an installed copy `%LOCALAPPDATA%\openm454`, `~/.local/share/openm454` elsewhere; `OPENM454_HOME` overrides it). It is not part of the repository.
- The package ships `openm454/calibration_placeholder.json`: a neutral placeholder (gain 1, every offset 0) with
  every entry present, to fill in with values measured on your unit.

| Command | Does |
|---|---|
| `openm454 cal` or `cal status` | the file in force: `unit_id`, `created`, `note`, `neutral`, `coverage` per mode, `awg` and `threshold`, `awg_output_range_v` (no USB) |
| `openm454 cal template --out FILE [--force]` | writes the neutral placeholder to fill in |
| `openm454 cal import FILE [--bind]` | validates the file and installs it atomically as `state/calibration.json`; `--bind` opens the scope and records this unit's `unit_id` |

### Using it

`--cal` is accepted by `capture`, `watch`, `stream`, `log`, `debug plan` (the shared vertical options) and every
`awg` subcommand (`set`, `sweep`, `burst`, `mod`, `dc`). An `--awg 'WAVE,...'` spec on `capture`, `watch`, `stream` and
`log` follows the command's `--cal`. The MCP server has a boolean `cal` (default false) on `capture`, `log_plan`,
`record_start` and `awg`.

With `--cal`:

- All volts are corrected: `capture`, `watch`, `analyze` and `plot` of saved files, the live lines and the result of
  `stream` and `log`, the saved `log` chunks, and the hook's `Block.volts` and `info['scale']`.
- The analog trigger level is converted with the corrected map.
- The digital threshold is programmed as (target - offset_v) / gain, within 0..5 V. One threshold DAC serves all eight
  inputs, so per-input offsets cannot be corrected. A target outside the reachable range is clamped with a warning, and
  the reachable switching voltage is reported.
- The generator levels are programmed as (v - offset_v) / gain. Requests outside 0..3 V are refused exactly as
  without `--cal`. A request inside 0..3 V that would need a programmed level outside 0..3 V (above 3 V when it exceeds gain x 3 V + offset_v, below 0 V when it is under a positive offset_v) is clamped; a clamp of more than half a generator step (about 3 mV) comes with a warning that
  gives the calibrated output range (for example, with gain 0.98 and offset_v 2 mV the output reaches 0.002 ..
  2.942 V; example numbers). Levels that clamp to the same value are refused. The reported `high_v` / `low_v` /
  `level_v` are the expected output.

### The model, fallbacks and limits

For each mode, channel and hardware V/div (as measured against a meter):

```
code - zero_code = gain * V / volts_per_code + zero_lsb + offset_slope_lsb_per_div * offset_div
V = ((code - zero_code) - (zero_lsb + slope * offset_div)) / gain * volts_per_code      (x probe factor)
```

`volts_per_code` = V/div x 8/235 (1 LSB); `offset_div` is the offset in divisions.

- A mode with one or two analog channels on and no entry falls back to the `34` entry, with a note and a warning. No
  entry at all: that channel is uncorrected, with a warning. Inverted channels stay uncorrected. AC coupling and
  `--bw20` are corrected but noted: the table is measured with DC coupling and full bandwidth.
- `log` without interleave (up to 100 MSa/s) uses the `34` table.
- With a 10x probe the table applies at the BNC; the probe's own ratio is not corrected.
- Keep inputs within about +-7 divisions absolute: the front end compresses above that with large offsets.

### Format

JSON: `format` `openm454-calibration`, `version` 1, `unit_id` (null or 16 lower-case hex digits), `created`, `note`, and

```
"analog": {mode: {channel: {vdiv_key: {"gain": g, "zero_lsb": z, "offset_slope_lsb_per_div": s}}}},
"awg": {"gain": g, "offset_v": o},
"threshold": {"gain": g, "offset_v": o}
```

- `mode`: `1` (one analog channel on), `2` (two), `34` (three or four). `channel`: `A0`-`A3`. `vdiv_key`: the hardware
  V/div at the BNC as `%g` of volts: `0.001`, `0.002`, `0.005`, `0.01`, `0.02`, `0.05`, `0.1`, `0.2`, `0.5`, `1`, `2`, `5`.
- Bounds, refused on load with `CAL_INVALID`:

  | Entry | Gain | Offset |
  |---|---|---|
  | analog | 0.8 .. 1.25 | `zero_lsb` within +-30, `offset_slope_lsb_per_div` within +-2 |
  | `awg` | 0.9 .. 1.1 | `offset_v` within +-0.1 V |
  | `threshold` | 0.8 .. 1.2 | `offset_v` within +-0.3 V |

### Unit binding

`unit_id` is the first 16 hex digits of the SHA-256 of the 512-byte EEPROM zero-info block (not secret; the security
key is never used). `cal import --bind` records it. A file bound to another unit is refused with `CAL_MISMATCH` before
anything is sent (`capture`, `watch`, `stream`, `log`, `awg`). A file without `unit_id` applies to any unit.

### Where it shows

- `applied.cal` in the output of `capture`, `stream` and `analyze` and in MCP summaries (for `log`: `plan.cal` in the
  plan, the `started` line and the result, and `applied.cal` in its saved files): `enabled`, `file`,
  `unit_id`, `created`, `neutral`, `mode`, `threshold` (`target_v`, `programmed_v`, and `requested_v`, `clamped` when
  clamped) and `analog`: per channel the record (`applied`, `mode`, `gain`, `zero_lsb`, `offset_slope_lsb_per_div`,
  `offset_div`, `zero_code_raw`, `volts_per_code_raw`, `note`), or `applied: false` with a `reason`.
  `applied.threshold_v` is the target and `applied.threshold_programmed_v` what was programmed.
- `awg.cal`: `requested_v`, `programmed_v`, `expected_v`, `gain`, `offset_v`, `unit_id`, `neutral`, and `clamped` /
  `warning` when clamped.
- Saved `.npz` files carry the meta `cal` and the corrected scale, so `analyze` and `plot` reconvert with the
  correction stored in the file, never with the current file. `plot` labels the y axis `V (calibrated)`.

### Making a table

Measure with a stable DC source and a reference meter on the same node as the inputs. For each mode, channel and V/div,
fit (mean code - zero code) against V / `volts_per_code` for `gain`, `zero_lsb` and a term linear in the offset. For
the generator: output = gain * programmed + `offset_v`. For the threshold: switching voltage = gain * programmed +
`offset_v`. Start from `cal template`, then `cal import FILE --bind`.

### Calibration errors

All exit 2: `CAL_MISSING` (no `state/calibration.json` with `--cal`, or a missing `cal import` file), `CAL_INVALID` (unreadable or failing
validation), `CAL_MISMATCH` (file bound to another unit). See Errors.

## Self-test

`python -m openm454 selftest` checks the scope itself. The M454 has a 1 kHz calibration output (a ~3 V square wave,
the probe-compensation signal) and the signal generator output, but no internal loopback: the inputs are not
connected to either inside the scope. The standard fixture is therefore the scope's own two outputs cabled to its
inputs, with no other instrument. The checks fall in two groups.

Wiring-free checks run on any wiring. They only read the inputs and never touch the generator (about 4-6 s):

- V01 device info
- V12 a forced capture of all 12 channels (A0-A3, D0-D7) at 100 MSa/s
- V13 the top rates and fold modes: 1 analog channel at 400 MSa/s, 2 analog at 200 MSa/s, 2 digital at 400 MSa/s, 4
  digital at 200 MSa/s
- V11 capture latency

Signal checks (V00, V02-V10: calibration square, V/div, sample-rate consistency, digital channels and threshold,
trigger position, digital trigger, no-trigger timeout, generator loopback) need the standard fixture below and are
reported as skipped, with the reason, when the wiring is not declared as that fixture. Their tolerances are, for
instance, 1 kHz +-2 %, top 2.8 V +-12 %, base 0 V +-0.15 V, duty 50 +-3 %.

| Input | Standard fixture |
|---|---|
| A0 | the scope's own signal-generator output (MCX, 0-3 V) |
| A1 (1x probe), A2 (10x probe), A3 (1x probe) | the scope's own 1 kHz calibration output (~2.8-3 V square, slow ~8 us RC rising edge, fast ~0.3 us falling edge) |
| D0, D2, D5, D7 | the same calibration output |
| D1, D3, D4, D6 | left open |

Declare the wiring once, in `state/fixture.json` of the data folder, or for a single run with `--fixture standard`:

```
python -c "from openm454 import fixture; fixture.save('standard', 'standard')"
python -m openm454 selftest                      # uses the recorded wiring
python -m openm454 selftest --fixture standard   # the fixture is wired right now (this run only)
```

`fixture.save(analog='standard', digital='standard', note=None)` writes `{"analog": ..., "digital": ..., "since": ...}`;
`"standard"` for a group of leads means the standard fixture (any other value means something else is connected and
the signal checks on those leads are skipped). A missing file means unknown wiring, so a fresh install runs only the
wiring-free checks; so does a file that cannot be read (the result's `note` says why; a UTF-8 byte-order mark is
fine). Update the file whenever the wiring changes.

The generator is used only with the analog fixture and without `--no-awg`: V09 parks it at 0 V for the no-trigger
timeout, and V10 drives a 2 Vpp sine and a 0-3 V square into A0, then turns it off (also when one of its captures
fails). `--no-awg` (and the MCP default) leaves the generator untouched; V10 is then left out of the results, and V09
is skipped when the last `awg` setting left the generator running (run `awg off` first to include it). `--quick` runs
fewer repetitions (about half the time). The result lists every check with `measured` and `expected`; the exit code
is 1 when a gating check failed.

## Which command for what

| Command | Use it for |
|---|---|
| `capture` | one record around an event or trigger, e.g. catch a brownout: `--trig A1:fall:3.0V --trig-mode normal --timeout 1h` |
| `watch` | repeated triggered snapshots with measurements (frequency, Vpp, ...) every N seconds |
| `stream` | untriggered recording with live statistics, analog and digital at their own rates, decoding the record |
| `log` | continuous gap-free recording or monitoring over minutes to hours: min / max / mean per interval, nothing missed between lines; with `--hook`, your own Python code sees every sample as it arrives and can raise events or stop the run (see [Live processing hook](https://github.com/FrostFlops/openm454#live-processing-hook-log---hook)) |

## Monitoring with `log`

`log` prints one statistics line per `--every` of signal time (default 1 s), so a long run is a table of
`vmin_v` / `vmax_v` / `vmean_v` per interval: dips and spikes show in `vmin_v` / `vmax_v` even when the mean hides
them. A 3.3 V rail for 8 hours, one line per ~10 s, nothing saved (`--storage none`):

```
python -m openm454 log -c A1 --vdiv 0.5V --offset 2.1V --rate 10k --duration 8h --every 10s --storage none
```

The window is ~0.09..4.09 V and one ADC step is ~17 mV (pick V/div as described under
[Limits and safety](https://github.com/FrostFlops/openm454#limits-and-safety)). Without `--storage none` every sample is kept in RAM (the default) and
written at the end: 72 MB for 2 h at 10 kSa/s, 288 MB for 8 h. Ripple on a rail: AC coupling and a small V/div,
which loses the DC level:

```
python -m openm454 log -c A1 --coupling ac --vdiv 20mV --rate 1M --duration 1h --every 1s --storage none
```

Check a plan first with `log --plan` (same options, no device). Over MCP: `log_plan`, then `record_start` with
`mode: "log"` (it takes `vdiv`, `offset` and `coupling` too), `record_status` (the last lines) and `record_stop`. A
recording started over MCP is stopped when the MCP client exits, so an unattended multi-hour log needs the Claude
session to stay open, or the CLI run in the background (`openm454 log ...`, ended with `openm454 stop`).

### How fast can `log` refresh?

`--every` can be as small as one 64-sample block, and lines are cut at exact signal-time boundaries (`--every` rounded to whole 64-sample blocks, see `plan.every_s`), not at USB transfers. Measured
on the M454 at 150 MB/s (A0-A1 + D0-D7 at 50 MSa/s, `--storage none`, 10 s each):

| `--every` | live lines | arrival gaps at the reader | scope backlog |
|---|---|---|---|
| 1 s | 1/s | ~0.94 s | 1.4 MB, flat |
| 100 ms | 10/s | ~103 ms | 1.4 MB, flat |
| 10 ms | 100/s | ~13 ms | 1.3 MB, flat |
| 1 ms | 1001/s | bursts, at most 8.6 ms apart | 1.2 MB, flat |

Printing 1000 lines/s while recording 150 MB/s is therefore not the limit (the lines carry statistics, not the samples). The data reaches the PC in USB transfers of ~1 MB (about 7 ms at
150 MB/s), so lines arrive in bursts and the earliest reaction is ~10 ms after the signal. The real limit is the
reader: an agent reading JSON lines or polling MCP `record_status` reacts in seconds at best. To react within
milliseconds, run your own code next to the data with a [hook](https://github.com/FrostFlops/openm454#live-processing-hook-log---hook).

### Live lines of `log` and `stream`

`log`: when this channel set's readout is measured first (above 150 MB/s and not yet known, or `--remeasure`), a
`probe` line; then a line with `"started": true`, the `plan` and `out_stem`; then one line per `--every`, then the
result. With `--hook`, event lines (`{"event": ...}`, no `i`) are interleaved as they happen; see [Live processing hook](https://github.com/FrostFlops/openm454#live-processing-hook-log---hook). Lines are cut at whole 64-sample blocks, so a line covers `--every` rounded to a multiple of 64 / rate (at
10 kSa/s `--every 10s` gives 9.9968 s); `plan.every_s` shows the value used. A live line has:

| Key | Meaning |
|---|---|
| `i` | line number from 0 |
| `t_signal_s` | signal time since the start of the recording, at the end of this line (samples / rate) |
| `t_wall_s` | PC clock seconds since the run started, when this data arrived (`t_wall_s - t_signal_s` is the lag) |
| `backlog_mb` | MB the scope has acquired but the PC has not read yet (an estimate); the run stops near `plan.backlog_stop_mb` (~503 MB): `HOST_TOO_SLOW`, or `ring_full` for a `--force` log planned to end there |
| `backlog_growth_mb_s`, `ring_full_in_s`, `gapfree_projected_s` | only while the backlog grows: MB/s over the last ~2 s, seconds until the guard stops the run, gap-free signal seconds the run will hold |
| `A0`..`A3` | `vmin_v`, `vmax_v` (whole ADC codes, LSB = V/div / 29.375) and `vmean_v`: volts at the probe tip over this line's interval only |
| `D0`..`D7` | `high_frac` (0..1, share of samples high), `edges` (transitions in this interval) |
| `chunk_saved` | disk storage: index N of the `chunk_NNNNN.npz` finished during this line |
| `ram_kept_s` | RAM ring (`--keep-last`, `forever`): seconds of signal now held |
| `final` | `true` on the last, possibly partial, line |
| `hook_ms` | with `--hook`: milliseconds spent in the hook since the previous line |

`stream`: lines have `i`, `t_signal_s`, `t_wall_s` (seconds since this record's start; it restarts at every record of
a chained or `forever` stream, while `t_signal_s` runs over the whole stream), `A0`..`A3` with `vmin_v`, `vmax_v`,
`vmean_v` and `n` (samples in this line), and `D0`..`D7` with `high_frac`, `edges`. A line is emitted once signal
time passes the next `--every` boundary, so it is cut at USB event boundaries. A single record ends with
`final: true`; a chained stream adds `record: k` to every line and `record_end: true` at the end of a record. There
are no backlog fields. The final result has the full measurements (`--measure`, `--window`, `--edges`, `--dump`), files,
hints and warnings; chained streams add `records`, `record_s`, `signal_s`, `seams_s`, `seam_max_s`, `stopped_by`.

## Live processing hook (`log --hook`)

A hook is a Python file of your own that sees every sample of a gap-free `log` recording as it arrives and can react
at once: a threshold alarm, an edge counter, a trigger for other equipment, an early stop. It is the way for an agent
to act on real-time data faster than it can read lines (see [Monitoring with log](https://github.com/FrostFlops/openm454#monitoring-with-log)).

```
python -m openm454 log -c A1,D0 --rate 1M --duration forever --storage none --hook myhook.py
```

The file is loaded before the scope is opened. Loading imports the file, so its top-level code runs. A missing file, a
syntax or import error, a `sys.exit()`, or a missing `process()` gives `INVALID_ARG` (exit 2) and nothing is recorded.
With `--plan` the file is loaded and checked the same way; no hook function is called and the scope is not touched.
The file runs as a registered module (dataclasses and the like work), its folder is added at the end of the import
path (helper modules next to it import), and `print()` in it goes to stderr, so stdout stays one JSON document per
line.

### The file

```python
def setup(info):      # optional, once before the first sample
    ...

def process(block):   # required: called for every piece of the recording, in order
    return None       # or a dict: an event

def finish(summary):  # optional, once at the end; a returned dict goes into the final result
    ...
```

`info` has `rate_hz`, `analog`, `digital`, `scale` (per analog channel: `volts_per_code`, `zero_code`, `probe`, `vdiv_v`),
`every_s`, `duration_s`, `storage`, `block_samples`, `piece_s` (the planned piece size in seconds). `summary` of `finish()` has
`samples`, `duration_s`, `stop_reason`, `calls`, `events`.

Every sample is passed exactly once, in order, without gaps, in pieces of about 10 ms of signal (shorter when `--every` is shorter, since a piece never spans more than one line;
longer when a USB transfer holds more, e.g. ~20 ms roll packets at slow rates; `info['piece_s']` is the planned size). Lines, statistics and storage are unchanged by the hook.

### The block

| Member | Meaning |
|---|---|
| `t0_s`, `t1_s` | signal seconds since the start at the beginning and end of the piece |
| `rate_hz`, `n` | sample rate and samples per channel in this piece |
| `analog`, `digital` | channel lists |
| `codes(ch)` | raw ADC codes (`uint8`) of an analog channel |
| `volts(ch)` | volts at the probe tip (`float32`) |
| `bits(ch)` | one `uint8` 0/1 per sample of a digital channel |
| `packed(ch)` | digital bits packed 8 per byte, little-endian |
| `times()` | `float64` signal time of every sample |

The arrays are the recording's own and read-only: an in-place edit (`c -= 128`) raises and ends the run with
`HOOK_ERROR`, the stored data unchanged. Compute on a copy (`c.astype(np.int16) - 128`). Pieces vary in length (`n`):
carry what you need across calls (the last sample, a running state) or an edge or crossing at a piece boundary is
missed.

### Events and stopping

`process()` returns `None` or a dict. A dict is an event and is printed at once as
`{"event": {...}, "k": n, "t_signal_s": ..., "lag_s": ..., "t_wall_s": ...}`: `t_signal_s` is the end of the piece,
`lag_s` how far behind live the hook was (seconds of newer signal the scope had already taken), `k` the event number
from 0, `t_wall_s` PC seconds since the run started. Put `"stop": true` in
the dict to end the recording cleanly (`acq.stop_reason` `hook`, `ok` true): the recording ends with that piece, and
nothing after it is kept or counted (in a RAM ring the history before the event stays). Any other return type is a
`HOOK_ERROR`.

The dict is printed as JSON: numpy scalars and arrays in it become plain numbers and lists, NaN and infinity become
`null`; anything else JSON cannot hold (a set, an object) is a `HOOK_ERROR`. The same holds for the dict `finish()`
returns.

Live lines get `hook_ms` (time spent in the hook since the last line). The final result gets a `hook` block: `file`,
`calls`, `events`, `seconds`, `load` (hook seconds per signal second), `lag_max_s`, `stopped_run`, `last_event` and
`result` (the dict `finish()` returned, if any). A `load` above 1 or a `lag_max_s` above 1 s adds a warning that the
hook fell behind.

### Hook errors

Any exception raised by hook code, `sys.exit()` and `KeyboardInterrupt` included, is a `HOOK_ERROR`. An exception in
`process()` ends the run with `HOOK_ERROR` (exit 2; `error.trace` has the traceback tail;
`acq.stop_reason` is `hook_error`). The data recorded up to and including that piece is kept and saved, and `finish()` is
not called. An exception in `finish()` also gives `HOOK_ERROR` with `error.trace`, but the recording is complete and
`acq.stop_reason` stays what ended it. An exception in `setup()` gives `HOOK_ERROR` (the message names the exception,
there is no `error.trace`) before anything is recorded. When the run ends with a device error or Ctrl+C, `finish()` is
still called and the error result carries the `hook` block.

### Hook performance

The hook runs in the thread that decodes the data, so it has to keep up. If it falls behind, the queue grows and the
host-speed guard stops the run with `HOST_TOO_SLOW` at 768 MiB (~805 MB) queued (data kept). Measured at 150 MB/s: a light hook
(count D0 edges with `bits()`, min/max of A1 codes, an event every 100 ms, stop at 2 s) made 144 calls in 2 s with
`load` 0.022, `lag_s` median 0.2 ms and max 7 ms, and stopped the run itself at 2.0 s. A heavy hook (`volts()` of both
analog channels plus an FFT per piece) had `load` 3.16, lag up to 2.2 s and a warning. So: use numpy on `codes()` /
`packed()` instead of `volts()` / `bits()` for every sample, keep the work per piece small, accumulate across pieces
in module-level state, and lower `--rate` or use fewer channels if needed.

### Hook safety

The hook must not talk to the scope: it is busy recording, so no `openm454` commands from the hook. Files, sockets,
serial ports and subprocesses are fine. A hook is your own code running with your rights: only run hook files you
wrote or trust.

### Hook over MCP and from Python

`record_start` takes `hook` (the absolute path of the file; a relative path is refused because the recording runs in
the MCP server's folder) with `mode: "log"`; with `mode: "stream"` it is `INVALID_ARG`. The hook's imports must be
installed in the Python that runs the MCP server.
`record_status` then adds `events` (the last `lines` event lines, default 3, at most 20) and `events_seen` (event lines in
the last 256 KiB of the output; the total is `hook.events` in the result of `record_stop`).
From Python: `logger.run(..., emit=print_or_collect, hook=openm454.hook.Hook(path))`; the event lines go to `emit`.

### Example: power-rail dip alarm

A1 on a 3.3 V rail; the hook reports the first sample below 3.0 V in each piece, comparing raw codes against a precomputed integer
limit, so no volts array is built per piece:

```python
import numpy as np
LIMIT_V = 3.0

def setup(info):
    s = info['scale']['A1']
    global LIMIT_CODE
    LIMIT_CODE = int(np.ceil(s['zero_code'] + LIMIT_V / (s['volts_per_code'] * s['probe'])))

def process(block):
    c = block.codes('A1')
    i = np.flatnonzero(c < LIMIT_CODE)
    if i.size:
        return {'dip_at_s': round(block.t0_s + i[0] / block.rate_hz, 6), 'min_v': round(float(block.volts('A1').min()), 4)}
```

```
python -m openm454 log -c A1 --vdiv 0.5V --offset 2.1V --rate 1M --duration forever --storage none --hook dip.py
```

Run on the M454 with A1 on the calibration square, which is below 3.0 V most of the time: one event per ~20 ms
piece at 1 MSa/s, `load` 0.005. The light and heavy hooks under
[Hook performance](https://github.com/FrostFlops/openm454#live-processing-hook-log---hook) were measured on the M454 too.

## Limits and safety

From the vendor datasheet:

| Input | Limit |
|---|---|
| analog BNC | -200 .. +200 V peak (DC + AC); 1 MOhm // ~16 pF; 8 bits, 1 mV/div .. 5 V/div |
| digital leads | -30 .. +30 V at the input end of the lead; 1 MOhm // ~16 pF; threshold 0..5 V (`--threshold`, default 1.4 V) |
| generator (MCX) | 0 .. 3 V |
| calibration output | ~3 V, ~1 kHz square |

- The probe ground is the PC's ground (through USB, and through the PC's mains earth if it has one). Connect it only to
  a point at the same potential, never to a "hot" ground or any point referenced to the mains.
- The on-screen range is not the safe range. At 1x the screen reaches about +-41.7 V (5 V/div with the offset at its
  limit), and at 10x about +-417 V, but the BNC limit is +-200 V peak and the probe's own rating applies.
- Running past the screen saturates the ADC; the data of that part is clipped.

Choosing V/div and offset for a DC rail with DC coupling: the screen shows 8 divisions, the farthest reachable edge
is +8.34 divisions, so use V/div >= V_rail / 8.34 and set the offset so the rail is on screen: with the default offset
0 V a positive rail above ~4 x V/div is off screen (at 1 V/div the window is -4.02..+3.98 V).

| Rail | V/div (1x) | Note |
|---|---|---|
| 3.3 V | 0.5 V | offset 2.0 V: window 0..4.0 V (2.1 V: 0.09..4.09 V; at 2.0 V a collapse to 0 V reads as 0 V); 0.2 V/div cannot reach 3.3 V |
| 5 V | 1 V | offset 2.5 V: about -1.5..6.5 V |
| 12 V | 2 V | offset 6 V: about -2..14 V |
| 24 V | 5 V | offset 12 V: about -8..32 V |
| above ~41 V | use a 10x probe | `--probe 10x`, limits x10; stay below the probe's rating |

## MCP server

`openm454-mcp` (or `python -m openm454.mcp_server`) serves the scope over the Model Context Protocol (stdio, no extra
dependencies; it is installed with `pip install openm454`):

```
claude mcp add -s user openm454 openm454-mcp
```

This form works in every shell (PowerShell, cmd, bash). `-s user` makes the server available in every folder
(without it, only in the folder where you ran the command). If pip's `Scripts` folder is not on `PATH`, give the
full path to `openm454-mcp`. DSWave is found by itself (search order above), or record it once with
`python -m openm454 setup --dswave <folder>`. Check with `claude mcp list`. Other clients:
`{"mcpServers": {"openm454": {"command": "openm454-mcp"}}}`.

An environment variable (`OPENM454_DSWAVE`, `OPENM454_HOME`) reaches the server only through the environment Claude
starts it with: give it when registering (`-e`). Claude starts the server in the background, so a variable set later
in a shell never gets there.
Put the server name before `-e` (`-e` takes several values and would swallow a name after it), then `--`, then the
command:

```
claude mcp add -s user openm454 -e "OPENM454_DSWAVE=<folder>" -- openm454-mcp
```

In Windows PowerShell, an npm-installed `claude` runs through a `.ps1` wrapper that drops a bare `--`, which gives
"missing required argument". Write the separator in quotes (`'--'`), call `claude.cmd`, use
`"--env=OPENM454_DSWAVE=<folder>"` with no separator, or run the command in cmd. Restart Claude afterwards. Each tool call runs one CLI command in a child process,
so the scope is opened and released per call, a hang never blocks the server, and every rule of the CLI applies.

11 tools:

| Tool | Runs | Key arguments (defaults) |
|---|---|---|
| `status` | `info` | none. Connected? model, USB speed, firmware / FPGA state, last generator setting |
| `capture` | `capture` | `channels` (`A0-A3,D0-D7`), `timebase` / `duration` / `rate` / `samples`, `trigger` (`SRC:EDGE[:LEVEL]`), `trig_mode` (`normal` with a trigger, `force` without), `timeout_s` (5), `trig_pos`, `holdoff`, `trig_retries` (4), `decode`, `edges`, `window`, `save` (true), `plot` (true), the vertical options (`vdiv`, `offset`, `probe`, `coupling`, `bw20`, `threshold`), `cal` (false). Per channel frequency, Vpp, min/max/mean, duty, rise/fall (digital: state, frequency, duty, edges), trigger result, decoded frames, file paths. A trigger that does not fire gives `TIMEOUT_NO_TRIGGER` |
| `analyze` | `analyze` | `file` (required), `channels`, `window`, `edges`, `decode`; measurements of a saved `.npz` |
| `plot` | `plot` | `file` (required), `channels`, `window`, `out`; returns the `.png` path |
| `awg` | `awg set` / `dc` / `off` / `status` | `action` (required: `set`, `dc`, `off`, `status`); `wave`, `freq`, `high`, `low` (or `vpp`, `dc`), `duty`, `phase`; `level` for `dc`; `cal` (false). The output keeps running after the call |
| `log_plan` | `log --plan` | `rate` (required), `channels`, `duration`, `every`, `storage`, `keep_last`, `force`, the vertical options; no device needed |
| `record_start` | `log` or `stream` in the background | `mode` (required: `log` or `stream`), `channels`, `rate`, `duration`, `every`, `storage` (log: `ram` / `disk` / `none`; stream: only `none`, nothing saved), `keep_last`, `force` and `hook` (log only; a hook file, `record_status` then returns its `events`), `decode` (stream only), the vertical options (`vdiv`, `offset`, `probe`, `coupling`, `bw20`, `threshold`), `cal` (false); returns a job id. One recording at a time, the scope is busy meanwhile |
| `record_status` | none (reads the job's output) | `job` (the latest), `lines` (3); latest status lines, and the result once done; with a `hook`, also `events` and `events_seen` |
| `record_stop` | `stop` | `job`, `wait_s` (120); stops cleanly and returns the result |
| `release` | `release` | none; frees a scope left running by a crashed program |
| `selftest` | `selftest` | `quick` (true), `fixture` (`file`; or `standard`), `awg` (false), `detail`, `max_wall_s` (1260) |

Results are summaries; on `capture`, `analyze` and `selftest`, `detail: "full"` returns the CLI's whole document. The
samples stay in the `.npz`. Every device call has a hard time limit, after which an error (`TIMEOUT_WATCHDOG`) is
returned and the CLI's own watchdog stops the scope: `capture` and `selftest` take `max_wall_s` (default for `capture`:
180 s plus the trigger timeout and the record length; for `selftest`: 1260 s, above the CLI's own 1200 s watchdog);
`status`, `awg` and `release` are limited to 60 s.

`selftest` runs `selftest --quick` (`--quick` dropped when `quick` is false), with `--no-awg` unless `awg` is true and
`--fixture standard` when `fixture` is `standard`, so by default it never touches the generator and only runs the
signal checks if the wiring is declared. Pass `fixture: "standard"` only after the user has wired and confirmed the
standard fixture, and `awg: true` only when the generator output goes to A0 and nothing else. The summary has
`passed_checks`, `failed_checks` (measured vs expected), `skipped_checks` (grouped by reason) and counts that match
those lists (checks not run after a device failure count as skipped), plus `fixture`, `note` and `seconds`. A failed
run returns the error `SELFTEST_FAILED`, whose hint tells a failed signal check (usually the wiring) from a failed
wiring-free one (the scope or its USB link); a device failure returns that device error (with `stopped_at`).

CLI commands with no MCP tool:

| Command | Why |
|---|---|
| `watch` | prints one JSON line per capture over many captures (20 by default, or until `--count` / `--for` / `--stop-when` / `openm454 stop`), which does not fit one request and one reply; use `record_start` with `mode: "stream"`, or call `capture` repeatedly |
| `setup` | one-time configuration of where DSWave is; run it once in a terminal: every later tool call reads the saved path (`state/dswave.json`) |
| `bench` | maintenance: measures this PC's USB readout speed, which `log` uses |
| `recover` | resets and reloads the FPGA of a hung scope; kept a deliberate CLI step |
| `debug` | raw register access, deliberately not exposed to agents |
| `stop` | `record_stop` uses it internally |
| `cal` | the calibration file is managed in a terminal (`cal template`, `cal import`); the tools only apply it with `cal: true` |
| `awg sweep`, `burst`, `mod` | the `awg` tool covers `set`, `dc`, `off`, `status` only |

An agent that has a shell can use the CLI for all of these.

## Errors

Every error has a code, a message and a hint, so the next step is clear:

| Code | Exit | Meaning |
|---|---|---|
| `NOT_FOUND` | 3 | the scope is not connected (USB 2A0E:0080 not seen) |
| `BUSY` | 4 | DSWave or another openm454 process holds the scope |
| `TIMEOUT_NO_TRIGGER` | 5 | nothing triggered within `--timeout` (`--trig-mode normal`) |
| `TRIGGER_OUT_OF_RANGE` | 2 | the trigger level is outside the channel's visible range (the message gives it); in normal and auto mode (with `--trig-mode force` the trigger is not used and the level is clamped) |
| `FIRMWARE_SOURCE` | 6 | DSWave 1.2.1 not found (or the file is not 1.2.1), so the FPGA bitstream cannot be extracted; the message lists what was searched, the hint names `openm454 setup --dswave <folder>` |
| `UNSUPPORTED` | 6 | the scope is connected at USB 2 speed; capture, watch, stream and log need a USB 3 (SuperSpeed) link |
| `USB_NO_MEM` | 6 | Linux only: the reads a capture queues do not fit the kernel usbfs buffer (`usbfs_memory_mb`, 16 MiB by default, shared by every USB device). Raised before the setup program when this capture alone exceeds the limit, at submit when another process holds part of it; the message has the queued bytes and the limit, the hint the one-line fix. A budget that cannot hold the geometry's queue is used for as many reads as fit, as long as the queue keeps at least 4 reads or at least 2 reads that hold 4 MiB (4 reads of a capture's ~1 MiB, 3 of ~2 MB, 2-3 of a live stream's 4-10 MB); capture, stream and log report the depth used as `acq.transfers`, and `acq.transfers_wanted` names the wanted depth when it was lowered (MCP summaries show both only then). At the stock 16 MiB: a fast capture queues 15 x 1 MiB, ~2 MB reads 8, `stream -c A0 --rate 400M` 2, `-c A0,D0 --rate 200M` 3; `-c A0,D0` at 400M and 12 channels at 100M are refused (they need `usbfs_memory_mb` >= 18 and >= 20; for stream, `--record-first` needs far less). The error comes only when not even the floor fits (nothing was written to the scope). A NO_MEM during a run is not reported as `USB_NO_MEM` but as `DEVICE_ERROR` (exit 6, the scope is left running and the next open stops it). `log --plan` shows the depth it asks for as `transfers_planned` |
| `INVALID_ARG` | 2 | an argument is wrong or impossible for this channel set, or the `log --hook` file cannot be used (missing, syntax or import error, no `process()`) |
| `STREAM_BETTER`, `NEEDS_FORCE` | 2 | `log` longer than its gap-free limit (see above) |
| `HOOK_ERROR` | 2 | `log --hook`: the hook file raised in `setup()`, `process()` or `finish()`, or `process()` returned something other than `None` or a dict; for `process()` / `finish()` `error.trace` has the traceback tail and the data recorded so far is kept; a `setup()` failure records nothing |
| `SELFTEST_FAILED` | 1 | MCP `selftest` only (the CLI exits 1 and lists the checks): a check failed; `failed_checks` has measured vs expected (a failed signal check usually means the wiring differs from the standard fixture, a failed wiring-free check points at the scope or its USB link) |
| `CAL_MISSING` | 2 | `--cal` given but there is no `state/calibration.json`, or `cal import` names a file that does not exist ([Calibration](https://github.com/FrostFlops/openm454#calibration-opt-in)) |
| `CAL_INVALID` | 2 | the calibration file is unreadable or fails validation (format/version, unknown mode, channel or V/div key, unit_id, a value out of bounds) |
| `CAL_MISMATCH` | 2 | the calibration file is bound to another unit; refused before anything is sent |
| `UNSAFE_OPERATION` | 7 | refused by the safety rules |
| `TIMEOUT_WATCHDOG`, `TIMEOUT_DATA`, `USB_ERROR`, `PROTOCOL_ERROR`, `HOST_TOO_SLOW` | 6 | the device or the host failed during a run; data up to then is kept |

## Layout

- `openm454/` driver: USB transport with a safety whitelist, device bring-up (FPGA, security handshake, calibration),
  register compiler (`mso.py`, reproduces DSWave's register programs exactly), stream decoder (`packet.py`),
  acquisition engine, loop-mode logger (`logger.py`), live processing hook for `log --hook` (`hook.py`), measurements, plots, AWG, CLI, self-test, protocol decoders
  (`decode.py`), synthetic bus traffic for tests (`protosim.py`), the opt-in PC-side calibration (`calib.py`, with the
  neutral `calibration_placeholder.json`) and the MCP server (`mcp_server.py`).
- `tests/` offline tests: golden register and AWG programs, the stream decoder, the acquisition engine against a
  simulated device, measurements, protocol decoders on synthetic traffic, the MCP server, safety rules
  (`python -m pytest tests`).
- `docs/PROTOCOL.md` the recovered USB protocol.
- `.github/workflows/`: CI (the offline tests on Windows and Linux, Python 3.10-3.14, with `OPENM454_NO_USB=1`, which
  makes the driver refuse to open USB; a check that tracked files carry no local paths; a package build) on pushes to
  main and on pull requests; a tag `vX.Y.Z`
  equal to `openm454.__version__` tests, builds and publishes to PyPI (trusted publishing) and creates a GitHub
  release.

## What is NOT here

The vendor application, its embedded FPGA bitstream and firmware images, decompiled vendor code, raw USB traces
(they contain the device's security key), the reverse-engineering workspace and the vendor datasheet are kept out of
this repository.

## Safety

The driver never sends the firmware-upgrade request (bRequest 0xB3), never writes the device EEPROM, and configures
the FPGA only when the device reports it unconfigured or when `openm454 recover` resets a hung scope (always from
the verified bitstream, as the first write of the session).

## License

GPL-3.0 (see `LICENSE`). DSView (GPLv3) by DreamSourceLab was used as a protocol reference. DSWave and DSCope are
trademarks of DreamSourceLab; this project is not affiliated with DreamSourceLab.
