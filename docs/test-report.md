# Test suite report

Base measurement (the PR #8 test-architecture change, 2026-11-17, Linux, Python 3.14.6, `origin/main` `47fcb6c`);
the current numbers of the merged tree are in the *Suite* table and in the paragraph below it. The baseline of that
base measurement is 823 passed, 554 skipped, 86.40 s, 26 test files, 8396 test LOC.

## Suite

| suite | command | result | wall time |
| --- | --- | --- | ---: |
| fast (PR) | `python -m pytest tests` | 888 passed, 554 skipped | **14.9 s** |
| clean software (no skips) | `python -m pytest tests -m "not hw and not slow and not traces"` | 888 passed | **14.9 s** |
| recorded material only | `python -m pytest tests -m traces` | 554 skipped without the recording | 1.9 s |
| full offline | `python -m pytest tests -m "not hw"` | 889 passed, 554 skipped | **65.5 s** |
| hw functional | `python -m pytest tests -m "hw and not hwsoak" -n 0 --bench-confirmed` | 53 passed on the AWG->A0 bench, never in CI | **24.8 s** |
| hw soak (60 s tier) | `python -m pytest tests -m hwsoak -n 0 --soak-seconds 60 --bench-confirmed` | 4 passed, artifacts in `re/soaks/` | **245.6 s** |
| model | `quint test specs/quint/capture.qnt --match=...` | 4 runs pass | 0.02 s |
| model invariants | `quint verify ... --max-steps=12` | 7 invariants, no violation | 0.64 s |
| fuzz | `python tests/fuzz/fuzz_packet.py -max_total_time=300` | 12.9 M execs, no crash | 301 s |

The fast suite is the default marker set (`not hw and not slow`) under `pytest-xdist -n auto --dist loadscope`;
the full suite adds the `slow` test. Every one of the 554 skips is a `traces` test: it compares against the DSWave
recording in `re/traces/S01_warm`, which .gitignore keeps out of the repository (the recorded runs contain the
device's security key). No firmware test skips. `-m "not traces"` therefore runs the software suite with no skips
at all (888 passed), and `-m traces` selects exactly the recorded-material group (554 today). One of them, the
sweep scenario `swp_lin_eq_5k_1s`, would skip even with the recording (every `time_step` line depends on the state
before it); it carries the marker so the software suite has no skips at all. What the recorded material is and how it was made: `docs/TRACES.md`.

Measured 2026-11-23 on the merged tree (0.4.0 plus `--bench-confirmed`, `docs/TRACES.md`, the two soak RSS
checks re-based below, and the four review rounds' fixes): fast **888 passed, 554 skipped, 14.9 s**, clean
software group **888 passed, 0 skipped**, full offline **889 passed, 554 skipped, 65.5 s**, hardware functional
**53 passed, 24.8 s** on the AWG->A0 bench, hardware soak (60 s tier) **4 passed, 245.6 s**, test LOC 8396 → 11628.
The two tables below stay the PR #8 measurement.

## Baseline vs. PR #8 (kept as that change's measurement)

| metric | baseline (`47fcb6c`) | PR #8 | note |
| --- | ---: | ---: | --- |
| collected | 1377 | 1386 | +9: properties and the capture machine |
| passed (fast) | 823 | 832 | |
| passed (full) | 823 | 833 | |
| skipped | 554 | 554 | unchanged fixture-driven skips |
| fast wall time | not selectable (86.4 s for everything) | **14.5 s** | excludes only the `slow`-marked 60 s MCP timeout test |
| full wall time | 86.40 s | 65.3 s | xdist + one golden runner |
| test files | 26 | 29 modules + 5 support + 2 fuzz + `conftest.py` (37 `.py`) | `tests/support` is not collected |
| test LOC | 8396 | 8843 | `git ls-files tests | grep '\.py$' | xargs cat | wc -l` (includes `support/`, `conftest.py`, `fuzz/`) |
| tests > 3 s | 1 | 1 | `tests/integration/test_mcp.py::test_log_plan_and_awg_arguments` (60.0 s, `slow`) |

Test LOC grew because Phase 3 added Hypothesis laws and the stateful machine, and because the shared support
package now owns the fakes, the USB simulator, the CLI helpers and the logger fakes (488 lines that used to be
inline in three test modules). The LOC target was "measure first, then decide": the consolidation removed the
duplicated AWG golden file and merged the DSWave loop-mode block into the runner without losing assertions.

## Per directory (fast / full, PR #8 measurement)

| directory | files | LOC | tests | skipped | fast | full |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 11 | 2336 | 276 | 0 | 3.79 s | 3.76 s |
| `tests/property` | 4 | 111 | 7 | 0 | 0.33 s | 0.33 s |
| `tests/stateful` | 4 | 2050 | 152 | 0 | 12.92 s | 12.91 s |
| `tests/golden` | 5 | 1788 | 838 | 548 | 0.14 s | 0.13 s |
| `tests/integration` | 5 | 1964 | 113 | 6 | 10.51 s | 70.19 s |
| `tests/support` | 5 | 488 | 0 | 0 | — | — |
| `tests/fuzz` | 2 | 84 | 0 | 0 | — | — |

Per-directory times are the junit sum of test durations, so they exceed the wall time under xdist.

## Slow tests

| test | time | handling |
| --- | ---: | --- |
| `tests/integration/test_mcp.py::test_log_plan_and_awg_arguments` | 60.02 s | `slow`, full suite only |
| `tests/integration/test_logger.py::test_bench_without_device_records_the_allowance` | 2.18 s | stays in the fast suite |
| `tests/integration/test_mcp.py::test_a_hung_command_never_hangs_the_call` | 1.83 s | stays in the fast suite |

## Hardware suite

`tests/hardware` runs against the real scope and is never part of CI: `hw` is outside the default marker set, and
with no device (or `OPENM454_NO_USB`, which CI sets) every test skips with the reason. What the suite assumes about
the bench is data, not prose: `tests/hardware/bench.json` declares each link — source, sink, probe attenuation,
volts/div, offset, the signal to put on the wire, and the tolerance it must be seen within. The capture
configuration *and* the expected measurement both come from that file, so a bench wired differently (a 10x probe in
line, a divider, another channel) fails and names the file instead of reporting a strange number; a link that is not
declared is never guessed — the test skips and lists what is declared.
`tests/unit/test_bench_declaration.py` checks the declaration offline, in the fast suite: every link complete,
physically consistent (the declared signal fits the declared screen and the generator's 0..3 V), and no hw test
asking for an undeclared link.

Both groups need `-n 0`: one device, one process (the conftest fails loudly instead of letting the other xdist
workers skip).

### Functional group (`hw and not hwsoak`)

53 tests, **24.8 s** on the bench. What it proves, all through the declared link:

- the connected scope is the declared model, and the declared 1 kHz / 3 V square arrives at 1 kHz and 3.1 Vpp
  (declared tolerance 0.3 V, still tight enough to reject the factor-of-10 error of a wrong probe or divider);
- every generator feature — sine, triangle, square duty 10/50/90 %, burst edge count, sweep period shortening, AM
  envelope, DC, off — measured by the capture, not asserted from the command;
- the rate ceilings (400/200/100 MSa/s for 1/2/3 channels), the 1024-sample floor, and the clamp of a rate above the
  ceiling, with the applied rate taken from the capture — plus a time-base check that goes through the signal: the
  same declared square measures the same frequency at 1 and 10 MSa/s (relative consistency only, since the generator
  and the sampler may share a clock);
- the capture modes: forced and triggered single, repeat forced (16 frames) and repeat triggered (8 frames, each
  with a trigger index and the declared rise at t = 0, and not eight copies), back-to-back captures that leave the
  device idle (the loop `watch` runs), a stream stopped on request, and `log` to RAM and to disk;
- trigger paths: rise and fall level triggers, the holdoff being waited out, and a level the signal never reaches
  reporting `TIMEOUT_NO_TRIGGER` (not a device error) with the scope usable afterwards;
- the real faults: a second run reports `BUSY` while the first completes, an overdriven input is reported and the
  declared signal comes back clean, a repeat run with no trigger says so and recovers, a stream stop leaves no
  submitted transfer;
- state: a capture adds no file under `state/`, the AWG record matches what was programmed, the engine is idle after
  every run;
- a link that is not declared skips with the declared names in the reason.

Six of these are the hardware counterpart of an offline test — the offline suite proves the logic, these prove the
device does it:

| offline test | hardware check on the bench |
| --- | --- |
| `tests/unit/test_selftest.py` (fake scope) | the driver's own self-test on the device: V01 and V12 (forced 5 ms capture of all 12 channels at 100 MSa/s) pass, and the standard-fixture checks skip **with the reason they need that fixture** — no silent pass, no false claim on this wiring |
| `tests/unit/test_timing.py` (keys, floors, refusals) | the floor profile is applied to the device: 186 ms per 1 ms capture with the traced spacing, **32 ms** with `gap=5 ms, quiet=5 ms, submit=5 ms, setup=2 ms, final=1 ms`, and the capture is still correct; a fixed key (`start=`) is refused with the scope idle |
| the plan's repeat hold invariant | after a repeat run the next capture still pays the traced **100 ms** hold even with `quiet=5 ms` (measured 127 ms vs 29 ms) — the profile cannot shorten an untriggered stop |
| `tests/unit/test_calib.py` | a table bound to this unit is applied (`applied.cal` present, identity table leaves the reading alone); a table from another unit is refused with `CAL_MISMATCH` and the scope stays usable |
| `tests/golden/test_packet_golden.py` (recordings) | a live frame has the planned depth and rate, and a triggered repeat frame's `trig_index` sits at the requested 50 % of the window (±2 %) |
| `tests/integration/test_cli_watch_measure.py` (saved files) | save → `load_capture` → `analyze` → `plot` on a real capture: the arrays and the measured numbers match, and the PNG is a real picture |
| `tests/unit/test_scope_paths.py` (refusals) | `stream`/`log` with repeat and a negative repeat are refused **with the device untouched** (`engine.active` false, no run), and a capture still succeeds afterwards |

### Long-run group (`hwsoak`, 60 s tier for the acceptance run)

4 runs, **245.7 s**, one artifact set per run in `re/soaks/` (`.meta`, `.jsonl`, `.exit`, `.err`,
`.summary.json`), every check's measured value in the summary:

| run | what it did | measured |
| --- | --- | --- |
| repeat | 49 captures x 512 frames | 25 088 frames, worst frame gap 2.17 ms, RSS +0.05 MiB after the warm-up, steady `insufficient_data` (49 samples) |
| stream | 162 records of 200 ms, 810 statistics rows | signal clock monotone, RSS +6.20 MiB after the warm-up, steady `insufficient_data` (162 samples) |
| log | 12 records x 5 s, to disk | 60 000 000 samples in 24 files, RSS +0.00 MiB after the warm-up, steady `insufficient_data` (12 samples) |
| lifecycle | open -> capture -> close, 63 cycles | open median 768.7 ms, RSS +41.01 MiB after the warm-up (the knee spans the 60 s tier's first quarter), steady `insufficient_data` (63 samples) |

Every run also asserts: no error row, frames/records counted, file descriptors and threads unchanged, the device
released and usable afterwards. Tiers are 5/10/60 min in `bench.json`; `--soak-seconds` overrides one run.

Two RSS checks, and neither may read allocator behaviour as a leak. 21 open/close cycles grow RSS by 26 MiB over the
first ~15 and then stay flat (measured), and a cold stream process climbs 57 -> 119 MiB over its first records, so
the growth of a whole short run is warm-up, not drift: `rss_after_warmup_mib` therefore compares the median of the
last quarter of the samples against the median of the first quarter and bounds what is left. The leak check is
`rss_steady_mib`, the larger of two robust terms inside the last quarter: the median pairwise slope times its span
(a slow climb every pair agrees on is reported at nearly its full height) and the median of the quarter's last tenth
against its first tenth (a step that stays is growth even when most of the quarter is still flat — 92 samples at
100 MiB and 8 at 200 MiB is 100 MiB retained, which the slope alone reports as 0). Each tenth is at least three
samples, so one sample above the plateau is a spike, not a step — a 63-sample 60 s lifecycle run has exactly that
few. A release is negative in both and floors at zero: a stream run that ends by freeing 23 MiB is not a leak. Growth
also has to be new territory: the stream run on this bench swings ~33 MiB (1.5 MiB paid per record, mostly returned),
so a quarter that climbs without passing the peak the run already reached is recovery, not growth (measured once: a
29.6 MiB slope in a run that ended 11 MiB under its own peak). The slope is estimated on at most 256 evenly spaced
samples of the quarter (32 640 pairwise slopes, ~2 MiB): a 1 h stream run has ~2 400 samples in its quarter, and the
full cross product cost 125 MiB at the end of the run for a number that the bounded estimate gives to within
0.003 MiB. Both are tri-state — `pass` / `fail` / `insufficient_data` — and the
steady one needs at least 200 samples, so the 60 s tier judges nothing: its runs hold 12-162 samples, and there the
26 MiB knee and the ~1.5 MiB per-cycle staircase are both larger than the 8 MiB bound — this bench measured 32-42 MiB
of net growth at 60 s against 11 MiB over 10 min and 1 h, so a 60 s verdict would be the knee, not a leak. All four
report `insufficient_data` there and the run still asserts the net bound, frame/record counts, gaps, fds, threads and
the released device. The 10 min tier has enough points for all four (measured below); the 1 h tier for the lifecycle
run only.

Measured at the default 10 min tier (2026-11-23, one session, 4 passed, 2404.7 s): stream +17.40 / steady 0.00
(1621 records), lifecycle +11.28 / steady 6.68 (626 cycles), repeat +0.06 / 0.04 (481 captures), log +0.004 / 0.00
(116 records). The warm-up is amortised as expected — stream's whole-run growth falls from 64.4 MiB at the 60 s tier
to 24.0 MiB here, lifecycle's from 64.1 MiB — so the old whole-run bound of 64 MiB was within 0.05 MiB of failing
the lifecycle run at this tier for allocator reasons alone. The lifecycle series is a staircase: ~1.5 MiB per
open/close cycle paid out and mostly released again (net ~0.024 MiB/cycle over the 470 cycles after the warm-up),
which is why its steady value (6.68) is the one number there close to the 8 MiB bound.

The 1 h tier was measured the same day on the lifecycle run (3764 cycles, 3601.8 s, passed): **+11.58 MiB** after the
warm-up, **steady 3.86 MiB**, open time 768.8 ms median with 0.04 % drift across the hour, fds and threads unchanged.
The staircase is not linear, so the 10 min run's ~0.024 MiB/cycle did not project: the first eighth carries the knee
(58 -> 124 MiB) and the remaining seven climb 124 -> 137, i.e. ~0.004 MiB/cycle over 3300 cycles, with occasional
1.5 MiB steps paid and returned (one eighth swings 11 MiB) — a projection from the shorter tier would have predicted
~90 MiB and a failure that did not happen. Why each reopen pays ~1.5 MiB that is mostly returned later is still open.

Seed defects, injected and reverted: dropping every frame's `host_s` fails the repeat run and the functional
arrival-time test; a stop path that forgets to release the device fails `test_the_engine_is_idle_after_a_capture`,
the back-to-back captures test and the repeat soak.

Bench-safety contracts, each verified on the device:

- **the generator is handed back, parameter by parameter**: the state file now records what was *asked* for
  (`high_req_v` / `low_req_v`, `level_req_v`) plus `duty_pct` / `phase_deg` for the burst, sweep and modulation modes,
  so the session fixture can rebuild the configuration instead of re-quantizing the reported levels (which drifted one
  6.4 mV step per restore). Verified by programming each of the seven modes (continuous, DC, burst, sweep, AM, FM,
  FSK), running a hardware test, and comparing the state file byte for byte: all seven come back identical. When the
  state at start was off or unknown the output is parked at 0 V (`output_v = 0`), and a failed restore parks as well
  and then raises — it is never a `pass`.
- **an explicit hardware run fails when the device is gone**: only `OPENM454_NO_USB` skips; a busy or missing scope
  is an error with the reason. Verified by holding the device lock from another process.
- **the disk budget is the run's own size, on the run's own filesystem**: `rate x duration` bytes with a 1.25 margin,
  measured on the path the data lands on (a one-hour log at 1 MSa/s is 3.6 GB, so a 1 GiB floor would have started a
  run that cannot finish). The rule is unit-tested offline in `tests/unit/test_soak_budget.py`.

## Formal model (Quint + Apalache)

- Model: `specs/quint/capture.qnt` — modes 0/1/2/3, repeat, stream/log, trigger/misfire/retry, no-trigger, stop.
- Invariants: `type_ok`, `no_repeat_in_stream_or_log`, `accepted_frames_only`, `misfire_discards_only`,
  `no_trigger_is_no_trigger`, `no_silent_fallback`, `quiet_after_untriggered_stop`; bounds `--max-steps=12`.
  Each of the three redefined ones was checked to be falsifiable: forcing a repeat run in mode 0, a no-trigger that
  keeps running, and a misfire that does not bump `discarded` each produces a counterexample.
- Quint 0.33.1, Apalache 0.63.0 (server on `localhost:8822`; without a server `quint verify` downloads its own
  pinned 0.62.4). `quint test` runs the four named executions in 20 ms; `quint verify` returns `[ok] No violation
  found` in 0.64 s.
- The first verification attempt found a real modeling bug (`log` set `mode = 3`); it is fixed in the model.
- Seeded-defect check: `specs/quint/check-seeded.sh` injects "a misfire counts as accepted" and requires a
  counterexample. The script writes its trace to a temporary directory, so it never dirties the worktree; the
  committed `specs/quint/counterexamples/accepted_frames_only.itf.json` is the recorded evidence from the first run.

## Fuzzing (Atheris)

| target | surface | 5 min run | corpus |
| --- | --- | ---: | --- |
| `fuzz_packet.py` | `packet.split_frames`, `packet.decode` with a 2 analog/2 digital repeat layout | 12,863,193 execs, 0 crashes | 4 seeds + 7 generated units |
| `fuzz_adapter.py` | `mso_adapter.decode` / `decode_frames` with a magic-valid head and a fuzzed trigger word | 12,841,387 execs, 0 crashes | 4 seeds + 17 generated units |

The nightly job runs the same targets with `-max_total_time=300` (5 min each, inside the 5-10 min budget).

Only `PacketError`/`ValueError` are accepted; anything else is a crash.

**These runs are crash-robustness checks, not sustained coverage-guided exploration.** The targets
import the driver inside `atheris.instrument_imports()`. The counters move a little at the start and then stay
put: the packet target ends at `cov: 72 ft: 72 corp: 1/1b` and the adapter at `cov: 115 ft: 115 corp: 3/165b`,
with no growth after the first minutes. Atheris instruments native code; the Python hooks it installs here do not
keep finding new paths. The targets are still worth running - millions of executions per minute against the
byte-level decoders with defined-error assertions - but no corpus-driven exploration is claimed.

## Seeded-defect acceptance (Python layers)

Each defect is injected into the driver, the whole fast suite is run, and the source is restored. All four are
caught by existing tests (no new test was needed):

| injected defect | caught by | failing tests |
| --- | --- | ---: |
| a misfire retry drops the retry's frames | `tests/stateful/test_trigger_check.py` | 5 |
| a triggered repeat without a head is reported as "the device did not start" | `tests/stateful/test_repeat_plumbing.py` | 3 |
| repeat mode allows `stream()` | `tests/stateful/test_capture_machine.py`, `tests/stateful/test_repeat_plumbing.py` | 2 |
| the untriggered-stop hold drops from 100 ms to the profile quiet time | `tests/stateful/test_acquire_sim.py` | 6 |

Representative tests: `test_misfires_are_discarded_and_rearmed`,
`test_a_triggered_repeat_without_a_head_is_no_trigger`, `test_stream_refuses_repeat_before_touching_the_device`,
`test_an_untriggered_stop_holds_the_traced_100ms_whatever_the_profile`.

## Deviations from the plan

- The golden layer is one table-driven runner for the AWG programs, the old repeat snapshot and the DSWave
  loop-mode setup block (`tests/golden/test_golden.py`). The recorded families that read their own traces
  (`test_mso_golden`, `test_packet_golden`, `test_awg_modes_golden`, `test_repeat_golden`) stay as separate
  table-driven files: they are already table-driven over their fixture, and merging them would rewrite hundreds of
  recorded assertions for no coverage gain.
- `tests/support/` owns every shared fake: `fakes.py` (scope fakes, `ScriptEngine`, the selftest scope), `sim.py`
  (the USB transport simulator used by the engine tests), `cli.py` (CLI JSON and watch helpers) and `logging.py`
  (loop-mode logger fakes). No test imports another test module any more.
- The stateful machine (`tests/stateful/test_capture_machine.py`) covers the decisions: misfire re-arm and
  the retry being returned, repeat mode as one unchecked start, the no-trigger error, the untriggered-repeat
  failure and the stream/log refusals. The engine hold times (the 100 ms stop) stay in
  `tests/stateful/test_acquire_sim.py`, where the fake transport can observe them; the machine does not pretend to
  cover them.
- `tests/property/test_packet.py` checks the layout algebra only; the round-trip and split-frame laws were dropped
  after they disagreed with the recorded layouts (they encoded an assumption, not the protocol).
- The spec job uses an Apalache server so `quint verify` does not download a second distribution inside CI.
- The old DSWave repeat snapshot and the new remote repeat trace coexist, as required; both are pinned.
- No hardware was used for this change. The timing-floor evidence is a recorded Linux/M454 1000/1000 run
  (0 errors, 499.55 s, 2026-11-17) with the floor profile `gap=5 ms, quiet=5 ms, submit=5 ms, setup=2 ms, final=1 ms`;
  the capture-spacing contract itself is unchanged by this branch.

## Reproduce

    python -m pytest tests                     # fast, 14.5 s, xdist
    python -m pytest tests -m "not hw"         # full offline, 65.3 s
    python -m pytest tests -m "not hw and not slow and not traces"   # software only, 0 skips
    python -m pytest tests -m traces           # the recorded-material group (needs re/traces/S01_warm)
    python -m pytest tests -m "hw and not hwsoak" -n 0 --bench-confirmed   # hardware, functional (24.8 s)
    python -m pytest tests -m hwsoak -n 0 --soak-seconds 60 --bench-confirmed   # hardware, long runs (tiers in bench.json)
    quint test specs/quint/capture.qnt --main=capture --match='repeat_ok|no_trigger_ok|stream_ok|log_ok'
    apalache-mc server &                       # then:
    quint verify specs/quint/capture.qnt --main=capture --init=init --step=step --max-steps=12 \
      --server-endpoint localhost:8822 --invariant=type_ok,no_repeat_in_stream_or_log,accepted_frames_only,misfire_discards_only,no_trigger_is_no_trigger,no_silent_fallback,quiet_after_untriggered_stop
    QUINT=quint APALACHE_SERVER=localhost:8822 specs/quint/check-seeded.sh
    python tests/fuzz/fuzz_packet.py -max_total_time=300 tests/fuzz/corpus/fuzz_packet
