# Behavior matrix

What the suite is supposed to cover, and which layer covers it. A critical row has at least two independent
layers: an example/golden oracle (left) and a generated one (right). Update this table when a behavior is added;
`docs/test-report.md` records the measured numbers.

| behavior | unit | property | stateful | golden | integration | fuzz | spec | hw |
| --- | :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| channel counts (1/2/4 analog, 1/2/4/8 digital, permutations) | ✅ | ✅ | ✅ | ✅ |  |  |  |  |
| timing: default profile, floor profile, fixed keys refused, untriggered 100 ms stop | ✅ | ✅ | ✅ |  |  |  |  |  |
| trigger: none, rise/fall, any, normal/auto/force, holdoff | ✅ |  | ✅ | ✅ |  |  |  |  |
| trigger: a misfire is re-armed and the retry is returned; a triggered run without a head is `TIMEOUT_NO_TRIGGER` |  |  | ✅ |  | ✅ |  | ✅ |  |
| repeat: 0/1/N frames, forced vs triggered, plan limits, mode-3 refusal | ✅ |  | ✅ | ✅ |  |  |  |  |
| repeat: one start, the frames are neither checked nor re-armed |  |  | ✅ |  |  |  |  |  |
| repeat: every frame keeps its host arrival time (npz meta `frame_host_s`, restored by `load_capture`, `frames_summary.host_s`; absent on a replay) |  | ✅ | ✅ |  |  |  |  | ✅ |
| stream/log: repeat is refused | ✅ |  | ✅ |  | ✅ |  | ✅ |  |
| stream/log: live vs record-first, chunk sizes, save/load | ✅ |  |  |  | ✅ |  |  |  |
| packet: head/tail, first vs later frame, block sizes, endianness/permutation, malformed input | ✅ | ✅ |  | ✅ |  | ✅ |  |  |
| setup/stop: setup program, start, stop, cross-table caches, repeated runs |  |  | ✅ | ✅ |  |  |  |  |
| stop: an untriggered stop leaves the run quiet (the 100 ms hold is in `acquire`) |  |  | ✅ |  |  |  | ✅ |  |
| interfaces: CLI, MCP, API, save/load, info/watch/log | ✅ | ✅ |  |  | ✅ |  |  |  |
| bench wiring: the declaration itself (complete, physically consistent, only declared links used) | ✅ |  |  |  |  |  |  |  |
| bench wiring: the connected scope is the declared model; the declared link shows the declared signal |  |  |  |  |  |  |  | ✅ |
| hw functional: capture modes, rates, trigger paths, and the real faults (BUSY, overdrive, no trigger, stop while streaming) all recover to a usable scope |  |  |  |  |  |  |  | ✅ |
| hw soak: long runs keep counting, stamping and releasing, without drift or growth (tiers 5/10/60 min, `--soak-seconds`) |  |  |  |  |  |  |  | ✅ |
| hw manual (TODO): unplug/replug, suspend/resume, usbfs exhaustion, power cycle — listed in `bench.json`, not implemented |  |  |  |  |  |  |  |  |
| hardware: real M454 golden traces (recorded bench runs, not a CI layer) |  |  |  |  |  |  |  |  |

## Notes on the marks

- **What the Quint model encodes:** modes 0/1/2, the repeat flag, running/streaming/logging, frame counters and a
  `quiet` flag. It does not encode payloads, the capture-spacing profile or its floors, plan limits, chunk sizes,
  save/load, cross-table caches or the 100 ms hold itself; the `spec` marks are only on the rows it encodes.
- **timing:** the profile floors are covered by unit, property and stateful tests; the model only knows that a stop
  returns to quiet.
- **host arrival:** `host_s` is stamped in the libusb completion callback (seconds since the start command, so the
  start settle is included) and `queue_us` is the event loop's delay after that; both measure the host side only, and
  neither is the device's sample or trigger time — the frame's own `trig_index` and content stay the authority. The
  property layer covers the save/load law and the no-stamp replay case, the stateful layer the engine loop that
  stamps (inside the run's wall clock) and the capture path that summarises it; there is no golden fixture with
  arrival times, because a replay has none.
- **groups:** the `traces` marker (registered in `pyproject.toml`) isolates every test that compares against the
  git-ignored DSWave recording, so they can be run or left out as a block: `-m traces` is the recorded-material
  group, `-m "not hw and not slow and not traces"` the software suite with no skips.
- **repeat:** repeat mode is one start; this baseline neither checks the frames for misfires nor re-arms them
  (`trigger_check_reason` says so), so there is no retry frame to keep. The misfire retry row is the normal
  (non-repeat) path.
- **channel counts and fuzz:** both fuzz targets use one fixed 2 analog / 2 digital layout, so they say nothing about
  other channel counts; the fuzz mark on the packet row is about the byte surface, not the layout.
- **hw:** `tests/hardware` runs against the real M454 (only with `--bench-confirmed`: the bench owner confirms for each run that the bench is wired as `tests/hardware/bench.json` declares; without it the run fails and lists the declared links) and is split in two groups by marker: `hw and not hwsoak`
  is the functional group (53 tests, 24.8 s) and `hwsoak` the long-run group (4 runs, tiers 5/10/60 min, overridable
  with `--soak-seconds`). One device, one process: both need `-n 0` (the conftest fails loudly under xdist). The
  default marker set and CI never select them; only `OPENM454_NO_USB` skips — a busy or missing scope is an error,
  and the session fixture hands the generator back mode by mode (levels, duty and phase included) or parks it. The
  long-run steady-RSS check takes the larger of the median pairwise slope inside the last quarter and the level
  of its last tenth against its first and the peak the run already reached, each tenth at least three samples so one
  sample is a spike and not a step (a step that stays is growth; a release is negative growth, not a leak; a quarter
  that climbs without passing the earlier peak is the allocator's swing, not growth) and is
  tri-state (`pass` / `fail` / `insufficient_data`), needs 200 samples, so the 60 s tier says it cannot judge
  (12-162 samples, less than the knee and the per-cycle staircase it would have to see past)
  instead of counting as verified; the growth after the allocator warm-up is bounded separately, by comparing the
  median of the first quarter of the samples with the median of the last. Seven of the functional checks are the device counterpart of an offline test
  (self-test, spacing profile and its repeat-hold invariant, calibration bound/foreign, live packet geometry and
  trigger index, save/load/analyze/plot, refusals leaving the device idle), so the offline logic and the device
  behaviour are covered by the same cases. The suite does not carry its wiring in constants: `tests/hardware/bench.json`
  declares every link (source, sink, probe,
  volts/div, offset, signal, tolerance) plus the soak budgets and the manual cases,
  `tests/unit/test_bench_declaration.py` checks the declaration offline — complete, physically consistent, no test
  using an undeclared link — and the hw tests take both the capture configuration and the expected measurement from
  it, so a differently wired bench fails with the file named.
- **old hardware row:** `test_selftest` is an offline fake-scope test and lives in `tests/unit/`; the last row
  stays documentation of recorded bench runs, not a CI layer.
- **selftest** appears in the interfaces row because it is a CLI/MCP-facing command with an offline oracle.
