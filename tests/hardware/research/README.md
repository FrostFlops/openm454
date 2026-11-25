# Research harness (run by hand, not by pytest)

Two experiments on a real M454, kept here so the numbers in the README and in `docs/SIGNAL-QUALITY.md` can be
reproduced instead of taken on trust. Neither file is collected by pytest (no `test_` prefix), and both drive the
scope directly.

| script | what it measures | output |
| --- | --- | --- |
| `usb_ceiling.py` | readout ceiling per channel configuration: logical rate, wire bytes, host-sustained rate, ring fill at the stop, and every EP 0x86 completion (bytes, reads still queued) for one configuration | `usb-ceiling.csv`, `usb-transfers.csv`, `manifest-usb_ceiling.json` |
| `signal_quality.py` | per-record sigma, amplitude, even/odd and mod-4 group means (interleave), rise and 50 % crossing, every configuration once per round in rotation (300 rounds shipped) | `signal-quality.csv`, `manifest-signal_quality.json`, plus a JSON summary with medians, bootstrap intervals and paired differences |

Run them on the bench (the scope attached, the generator wired to the declared input):

    python tests/hardware/research/usb_ceiling.py --bench-confirmed                # ~30 s
    python tests/hardware/research/signal_quality.py --bench-confirmed --rounds 300  # ~7 min, 1800 captures

Both drive the generator into the declared input, so both ask for the same per-run confirmation the hardware suite
does: without `--bench-confirmed` they stop before opening the scope and print the links `bench.json` declares.

Both read the declared link from `tests/hardware/bench.json` (channel, probe) and set their own levels: the signal
quality experiment deliberately uses 0.20 V at 0.05 V/div so that 1 LSB = 1.7 mV, which the 3 V declared signal could
not show.

## What the CSV files are

- `usb-ceiling.csv` — one row per configuration: `logical_mb_s` (rate × (analog + digital/8)), `wire_mb_s` (bytes
  received / signal time), `host_mb_s` (bytes received / wall time), `backlog_mb` (the loop ring's fill at the stop),
  the queue depth used and the wall time. The three rates differ on purpose; see the README section for why.
  `wire_mb_s` counts bytes the **host received** against the signal duration — it is not a bus-analyzer measurement of
  the link, so it checks the data geometry and the application-level rate, not USB bus utilisation.
- `usb-transfers.csv` — the raw pacing evidence for one configuration: `t_s` since the first completion, `bytes`
  of that transfer, and how many reads were still queued when it completed.
- `signal-quality.csv` — one row per capture: round, configuration, applied rate, sigma, amplitude, even/odd, mod-4,
  10–90 % rise, 50 % crossing position.

## Each run writes a manifest

`manifest-<script>.json` records the session id, the unit (model, firmware, panel, FPGA, link speed), the kernel and
platform, the git commit, the SHA-256 prefixes of the script and of `bench.json`, and the arguments. Without it two
CSV files cannot be told apart from two different units or script versions.

The generator is handed back at the end of both scripts (`tests/support/bench.py:restore_or_park`): the state found
at the start is restored, and if that fails the output is parked at 0 V and the script exits non-zero.

## Caveats

- One bench, one unit, one generator. The generator and the scope may share a clock; nothing here is an absolute
  time-base calibration.
- The signal-quality numbers compare configurations **within** a run: the absolute level drifts a few percent between
  sessions, the paired differences do not.
