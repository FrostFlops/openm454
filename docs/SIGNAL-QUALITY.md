# Signal quality vs. the channel configuration (measured 2026-11-18)

The question: **at the same per-channel sample rate**, does enabling more channels change what the scope measures on
one channel? Answer: the differences are real but stay below a quarter of one code, and their size moves between
sessions: two to four analog channels cost **+0.14 .. +0.21 mV of sigma** (0.08-0.12 LSB) and the eight digital
channels cost **-0.03 .. -0.04 mV** (indistinguishable from zero). The one-channel mode is a different acquisition
path — 1.28 LSB higher gain, 0.5 LSB of interleave mismatch.

The control variable matters. This page varies the **channel count at a fixed rate** and compares only where the
*applied* rates match (a request of 50 MSa/s lands on 40 and 100 on 80 for a single channel). The other axis — the
same wire throughput reached with different channel sets (1 analog at 400 MSa/s vs 4 at 100 MSa/s, both 400 MB/s) —
is confounded by the per-channel rate and is not used for any conclusion here.

## Method

One M454, USB 3. Only the declared link's channel carries a signal (the built-in generator: 1 kHz square, 0–0.20 V);
the other analog inputs are left open and the digital inputs unconnected — they still produce samples, which is what a
configuration costs.

- 0.05 V/div, 0.10 V offset: **1 LSB = 1.7 mV**; the 214 mV square is ~126 codes.
- 10 ms records, triggered on the rise at 0.10 V, so the edge sits at t = 0.
- Per record: flat-region sigma (max of the two plateaus), the amplitude between plateaus, the even/odd and mod-4
  group means of the high plateau (interleave mismatch), 10–90 % rise, 50 % crossing position.
- **300 rounds, every configuration once per round in rotation.** The rotation is the point: the absolute level drifts
  between sessions (two runs of this same script moved the median sigma of the 4-analog set from 2.71 to 2.61 mV,
  ~4 %), so only differences taken **inside a round** are reproducible.

## Results

Absolute medians of the 300 records per configuration of the shipped run (session 20261118-172247), with a
bootstrap 95 % interval (mV):

| configuration | applied rate | sigma | amplitude | even/odd | mod-4 | rise (ns) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 analog | 400 MSa/s | 1.92 [1.87, 1.94] | 214.25 | 0.07 | **0.95** | 85 |
| 1 analog | 200 MSa/s | 1.94 [1.90, 1.97] | 214.56 | **0.95** | 0.97 | 85 |
| 2 analog | 200 MSa/s | 2.27 [2.25, 2.29] | 212.36 | 0.17 | 0.19 | 85 |
| 2 analog | 100 MSa/s | 2.23 [2.21, 2.26] | 212.34 | 0.01 | 0.06 | 90 |
| 4 analog | 100 MSa/s | 2.44 [2.41, 2.47] | 212.58 | 0.01 | 0.06 | 90 |
| 4 analog + 8 digital | 100 MSa/s | 2.41 [2.38, 2.44] | 212.60 | 0.01 | 0.06 | 90 |

The absolute level is session specific: four 300-round sessions gave 1.92-2.06 (1 analog), 2.23-2.43 (2 analog) and
2.41-2.60 mV (4 analog) — up to 15 % apart. That is why the results below are paired differences, never absolute
levels.

Paired differences within a round (later minus earlier; an interval that excludes 0 is resolvable):

| from | to | sigma | in LSB | amplitude | in LSB |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 analog @200 | 2 analog @200 | +0.25 [+0.19, +0.28] | 0.15 | -2.18 [-2.20, -2.17] | 1.28 |
| 2 analog @100 | 4 analog @100 | +0.14 [+0.09, +0.19] | 0.08 | +0.24 [+0.23, +0.25] | 0.14 |
| 4 analog @100 | 4 analog + 8 digital @100 | -0.03 [-0.05, -0.01] | 0.02 | +0.01 [-0.01, +0.02] | 0.01 |
| 1 analog @200 | 1 analog @400 | +0.01 [-0.03, +0.05] | 0.01 | -0.32 [-0.33, -0.31] | 0.19 |

These intervals are the within-session ones; the between-session spread is larger (next section), so a result is
quoted as a range over sessions rather than as one interval.

## What it means

1. **Adding the eight digital channels costs nothing measurable** on the analog channel: sigma -0.03 to -0.04 mV
   across sessions (0.02 LSB), amplitude +0.00 to +0.01 mV.
2. **Two to four analog channels: resolvable, sub-code, and always positive.** sigma +0.14 to +0.21 mV (0.08-0.12 LSB)
   in every session measured, amplitude +0.24 to +0.26 mV (0.14-0.15 LSB).
3. **The one-channel mode is a different acquisition path.** Amplitude is **1.28 LSB (1.0 %) higher**, sigma 0.22 LSB
   lower, and the interleave mismatch is **0.95 mV (0.56 LSB)** where the multi-channel records show 0.02–0.25 mV.
   At 200 MSa/s it sits in the even/odd (lag 2) means; at 400 MSa/s it moves to the mod-4 means (lag 2 is 0.06) —
   a 2-way and a 4-way interleaved converter respectively. That is an inference from the group means, not a
   die-level proof of the topology.
4. **Interleave mismatch does not grow with the channel count**: 2 and 4 analog channels show the same 0.02 mV.

## Repeatability, stated so the numbers are not over-read

Four sessions, each with every configuration once per round, give the paired differences below (median, mV):

| session | rounds | 1 → 2 analog | 2 → 4 analog | 4 analog → + 8 digital | amplitude 1 → 2 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-11-18 a | 60 | +0.42 | +0.21 | +0.00 | -2.12 |
| 2026-11-18 b | 300 | +0.35 | +0.19 | -0.04 | -2.13 |
| 2026-11-18 c | 300 | +0.42 | +0.21 | +0.01 | -2.12 |
| 2026-11-18 d (shipped) | 300 | +0.25 | +0.14 | -0.03 | -2.18 |

The between-session spread of the effect (+0.25 to +0.42 mV for one to two channels) is larger than the
within-session interval (+-0.05 mV), so the honest statement is the range, not one interval: the effect is always
positive, its size depends on the session (front-end and generator conditions). Absolute sigma moves 15 % between
sessions for the same reason.
- The per-record sigma has a heavy tail: a robust scale flags ~10 % of the records well above the median (up to 9 mV
  during a transient disturbance). That is why the summary uses medians and bootstrap intervals.
- Only the declared channel carries a signal. The open inputs see about 60 mVpp of the driven square (measured
  separately), independent of how many channels are enabled.
- The 82–90 ns rise is the generator's edge, not the front end: nothing here measures the 50 MHz analog bandwidth.
- The digital channels are unconnected: their own levels are not a quality measurement.

## Reproduce

`tests/hardware/research/signal_quality.py` runs the rounds, writes `signal-quality.csv` (one row per capture:
round, configuration, applied rate, sigma, amplitude, even/odd, mod-4, rise, edge position) and prints the per-config
medians with bootstrap intervals plus the paired differences:

    python tests/hardware/research/signal_quality.py --bench-confirmed --rounds 300   # ~7 min on one bench

Both scripts drive the generator, so both require `--bench-confirmed` for the run (the same per-run wiring
confirmation the hardware tests ask for) and restore the generator afterwards.
`tests/hardware/research/usb_ceiling.py` produces the readout-ceiling table and the transfer trace quoted in the
README. Both are run by hand on a bench with the scope attached; neither is part of pytest.
