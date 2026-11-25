"""The soak disk budget is checked offline: the rule that decides whether a long run may start."""
import tempfile
from pathlib import Path

from tests.support import soak as soakmod


def test_a_path_that_does_not_exist_yet_is_measured_on_its_parent():
    root = Path(tempfile.mkdtemp())
    missing = root / "does" / "not" / "exist"
    assert soakmod.free_disk_mib(str(missing)) > 0
    assert soakmod.free_disk_mib(str(missing)) == soakmod.free_disk_mib(str(root))


def test_the_budget_uses_the_whole_run_and_a_margin(monkeypatch):
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 100.0)
    ok = soakmod.disk_budget("/tmp/soak", 50 * 1024 * 1024, margin=1.25)
    assert ok["ok"] and ok["need_mib"] == 50.0 and ok["margin"] == 1.25
    # 100 MiB free is not enough for 50 MiB x 1.25 when the free space is 60
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 60.0)
    tight = soakmod.disk_budget("/tmp/soak", 50 * 1024 * 1024, margin=1.25)
    assert not tight["ok"] and "60.0 MiB free" in tight["reason"]


def test_a_one_hour_log_does_not_start_with_a_gigabyte_floor(monkeypatch):
    """1 MSa/s for 3600 s is 3.6 GB: a 1 GiB floor would have started a run that cannot finish."""
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 1024.0)
    need = 1e6 * 3600
    budget = soakmod.disk_budget("/tmp/soak", need, margin=1.25)
    assert not budget["ok"] and budget["need_mib"] > 3000


def test_steady_growth_is_tri_state():
    """pass / fail / insufficient_data: a short series must not count as verified."""
    short = soakmod.steady_growth([1.0] * 10, 8.0)
    assert short["state"] == "insufficient_data" and short["ok"] is None
    flat = soakmod.steady_growth([1.0] * 40, 8.0)          # the last quarter is flat
    assert flat["state"] == "pass" and flat["ok"] is True and flat["value"] == 0.0
    leaking = [1.0] * 30 + [float(i) for i in range(1, 11)]
    leak = soakmod.steady_growth(leaking, 8.0)             # the last quarter climbs by 9 MiB
    assert leak["state"] == "fail" and leak["ok"] is False and leak["value"] == 9.0


def test_a_release_inside_the_last_quarter_is_not_a_leak():
    """The stream soak on this bench ends by releasing 23 MiB; a peak-to-trough range reads that as growth."""
    plateau_then_release = [179.7] * 70 + [156.3] * 12
    released = soakmod.steady_growth(plateau_then_release, 8.0)
    assert released["state"] == "pass" and released["ok"] is True and released["value"] <= 0.0


def test_a_step_that_stays_is_growth_even_when_most_of_the_tail_is_flat():
    """Offline reproduction from the review: 92 samples at 100 MiB, then 8 at 200 MiB.

    Both medians (the pairwise slope and the first/last quarter for the total) stay at the plateau, so the old pair
    reported 0 MiB and let 100 MiB of retained memory through; the level term sees the last tenth of the window.
    """
    late_step = [100.0] * 92 + [200.0] * 8
    steady = soakmod.steady_growth(late_step, 8.0)
    assert steady["state"] == "fail" and steady["ok"] is False and steady["value"] == 100.0


def test_a_quarter_that_climbs_without_passing_the_peak_is_recovery():
    """The 60 s stream run on this bench pays ~1.5 MiB per record and mostly returns it: the swing is ~33 MiB.

    Measured once: the last quarter climbed 23 MiB (a 29.6 MiB slope) while ending 11 MiB under the peak the run had
    already reached, and the check failed a run that had not grown. Growth has to be new territory.
    """
    under_the_peak = [190.0] * 30 + [160.0] * 45 + [160.0 + 1.5 * i for i in range(20)] + [190.0] * 5
    recovered = soakmod.steady_growth(under_the_peak, 8.0)
    assert recovered["state"] == "pass" and recovered["value"] == 0.0
    new_territory = [190.0] * 30 + [160.0] * 45 + [160.0 + 2.5 * i for i in range(20)] + [210.0] * 5
    grown = soakmod.steady_growth(new_territory, 8.0)
    assert grown["state"] == "fail" and grown["value"] > 8.0


def test_an_early_spike_does_not_hide_a_later_climb():
    """The review's case: one sample at 180 MiB at the start used to set the level the run had already reached, so a
    tail climbing 100 -> 136 MiB read as 0 MiB and passed. A level has to be held for a window."""
    climb = [100.0] * 61 + [100.0 + i for i in range(36)] + [136.0] * 3
    plain = soakmod.steady_growth(climb, 8.0)
    spiked = soakmod.steady_growth([180.0] + climb[1:], 8.0)
    assert plain["state"] == "fail" and spiked["state"] == "fail"
    assert spiked["value"] == plain["value"], 'the spike is not a level: it changes nothing'


def test_the_slope_uses_at_most_the_declared_number_of_points():
    """The review's case: 511 samples in the quarter, floor division gave step 1 and 130 305 pairwise slopes."""
    for n in (511, 941, 2430):
        pts = soakmod.slope_series(list(range(n)))
        assert len(pts) <= soakmod.SLOPE_POINTS, n
        assert len({b[0] - a[0] for a, b in zip(pts, pts[1:])}) == 1, 'evenly spaced'
        assert pts[0][0] == 0 and pts[-1][0] < n
    assert len(soakmod.slope_series(list(range(511)))) == 256


def test_a_single_sample_at_the_end_is_not_growth():
    """Offline reproduction from the review's second round: 62 samples at 100 MiB and one at 140 MiB.

    The window of a 63-sample 60 s lifecycle run is that short, and a one-sample window read the spike as a 40 MiB
    step. Each tenth is at least three samples now, so one sample cannot move the median.
    """
    spike_at_the_end = [100.0] * 62 + [140.0]
    steady = soakmod.steady_growth(spike_at_the_end, 8.0)
    assert steady["state"] == "pass" and steady["value"] == 0.0
    two_samples = [100.0] * 61 + [140.0, 140.0]          # two of three still move the median: that is a step
    assert soakmod.steady_growth(two_samples, 8.0)["state"] == "fail"


def test_one_spike_does_not_decide_the_steady_verdict():
    """A single sample 40 MiB above the plateau is not a trend: the median pairwise slope stays at the plateau's."""
    spiked = [120.0] * 60 + [160.0] + [120.0] * 19
    steady = soakmod.steady_growth(spiked, 8.0)
    assert steady["state"] == "pass" and steady["value"] == 0.0


def test_net_growth_leaves_the_allocator_warmup_out():
    """A cold stream process climbs 62 MiB over its first records and stays flat after that: not a leak, and not
    something a whole-run bound can tell apart from one."""
    warmup_then_flat = [50.0 + 2 * i for i in range(20)] + [119.0] * 60
    net = soakmod.net_growth(warmup_then_flat, 64.0)
    assert net["state"] == "pass" and net["value"] == 50.0          # the whole run grew 69 MiB: the bound is 64


def test_net_growth_fails_on_a_step_that_stays():
    stepped = [100.0] * 40 + [180.0] * 40
    net = soakmod.net_growth(stepped, 64.0)
    assert net["state"] == "fail" and net["ok"] is False and net["value"] == 80.0
    down = soakmod.net_growth([180.0] * 40 + [156.0] * 40, 64.0)   # a release is negative growth
    assert down["state"] == "pass" and down["value"] == -24.0


def test_net_growth_is_tri_state():
    short = soakmod.net_growth([1.0, 2.0, 3.0], 64.0)
    assert short["state"] == "insufficient_data" and short["ok"] is None