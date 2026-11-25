"""Opt-in calibration (openm454.calib): file format, the analog / threshold / generator corrections, metadata,
saved captures, CLI and MCP plumbing. No device."""
import copy
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from openm454 import calib, scope as scopemod, logger, mcp_server, cli
from openm454.capture import Capture, load_capture, CODE_MAX
from openm454.errors import DscopeError
from openm454.scope import Settings, hal_config

PKG = os.path.dirname(calib.__file__)


def table(**over):
    """A non-neutral table: A0 at 1 V/div corrected in every mode (mode 1 differs), plus generator and threshold."""
    d = calib.placeholder()
    d['analog']['34']['A0']['1'] = {'gain': 1.03, 'zero_lsb': -0.5, 'offset_slope_lsb_per_div': -0.1}
    d['analog']['1']['A0']['1'] = {'gain': 1.04, 'zero_lsb': -0.8, 'offset_slope_lsb_per_div': -0.2}
    d['analog']['2']['A0']['1'] = {'gain': 1.02, 'zero_lsb': -0.6, 'offset_slope_lsb_per_div': -0.1}
    d['awg'] = {'gain': 0.9896, 'offset_v': 0.00045}
    d['threshold'] = {'gain': 0.949, 'offset_v': 0.030}
    d.update(over)
    return d


def install(doc):
    calib.save(doc)
    return doc


def st_(analog=('A0',), digital=(), vdiv=1.0, offset=0.0, cal=True, **kw):
    st = Settings()
    st.analog, st.digital = list(analog), list(digital)
    st.vdiv = {c: vdiv for c in st.vdiv}
    st.offset = {c: offset for c in st.offset}
    st.duration, st.rate = 1e-3, 1e6
    st.trig_source, st.trig_mode = None, 'force'
    st.cal = cal
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ------------------------------------------------------------------ file
def test_placeholder_is_neutral_valid_and_matches_the_packaged_file():
    p = calib.placeholder()
    assert calib.validate(p) == [] and calib.neutral(p)
    for m in calib.MODES:
        for c in calib.CHANNELS:
            assert sorted(p['analog'][m][c], key=float) == sorted(calib.VDIV_KEYS, key=float)
    shipped = json.load(open(os.path.join(PKG, 'calibration_placeholder.json'), encoding='utf-8'))
    assert shipped == p


@pytest.mark.parametrize('mut, word', [
    (lambda d: d.update(format='x'), 'format'),
    (lambda d: d['analog']['34']['A0'].update({'1': {'gain': 0.5}}), 'gain'),
    (lambda d: d['analog']['34']['A0'].update({'1': {'gain': 1.0, 'zero_lsb': 99}}), 'zero_lsb'),
    (lambda d: d['analog']['34']['A0'].update({'3': dict(calib.NEUTRAL_ENTRY)}), 'V/div key'),
    (lambda d: d['analog'].update({'7': {}}), 'mode'),
    (lambda d: d.update(awg={'gain': 2.0, 'offset_v': 0}), 'awg.gain'),
    (lambda d: d.update(threshold={'gain': 1.0, 'offset_v': 1.0}), 'threshold.offset_v'),
    (lambda d: d.update(unit_id='XYZ'), 'unit_id'),
    (lambda d: d.update(bw20_gain={'5': 0.99}), 'bw20_gain mode'),
    (lambda d: d.update(bw20_gain={'1': 0.5}), 'bw20_gain.1'),
    (lambda d: d.update(bw20_gain=0.98), 'bw20_gain must be an object'),
])
def test_validate_refuses_typos(mut, word):
    d = calib.placeholder()
    mut(d)
    probs = calib.validate(d)
    assert probs and any(word in x for x in probs)


def test_read_missing_and_invalid(tmp_path):
    with pytest.raises(DscopeError) as e:
        calib.read(str(tmp_path / 'none.json'))
    assert e.value.code == 'CAL_MISSING' and e.value.exit_code == 2
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    with pytest.raises(DscopeError) as e:
        calib.read(str(bad))
    assert e.value.code == 'CAL_INVALID'
    d = calib.placeholder()
    d['awg']['gain'] = 5
    bad.write_text(json.dumps(d))
    with pytest.raises(DscopeError) as e:
        calib.read(str(bad))
    assert e.value.code == 'CAL_INVALID'


def test_save_is_atomic_and_refreshes_the_cache():
    install(table())
    assert calib.active()['awg']['gain'] == 0.9896
    d = table()
    d['awg']['gain'] = 1.01
    calib.save(d)
    assert calib.active()['awg']['gain'] == 1.01
    assert not [f for f in os.listdir(os.path.dirname(calib.PATH)) if f.endswith('.tmp')]


def test_unit_binding():
    zi = bytes(range(256)) * 2
    uid = calib.unit_id(zi)
    assert len(uid) == 16 and calib.unit_id(zi) == uid
    assert calib.unit_id(None) is None and calib.unit_id([object()]) is None    # no raw block: unknown, no crash
    calib.check_unit(dict(table(), unit_id=None), zi)              # unbound: any unit
    calib.check_unit(dict(table(), unit_id=uid), zi)
    with pytest.raises(DscopeError) as e:
        calib.check_unit(dict(table(), unit_id='0' * 16), zi)
    assert e.value.code == 'CAL_MISMATCH'


# ------------------------------------------------------------------ analog
def test_without_cal_nothing_changes():
    install(table())
    for st in (st_(cal=False), st_(cal=False, trig_source='A0', trig_mode='normal', trig_level=0.7)):
        hal, scale, w, _ = hal_config(st)
        assert 'cal' not in hal and 'cal' not in scale['A0'] and isinstance(scale['A0']['zero_code'], int)


def test_neutral_placeholder_changes_no_register_and_no_volt():
    install(calib.placeholder())
    for kw in ({}, {'trig_source': 'A0', 'trig_mode': 'normal', 'trig_level': 0.73}, {'offset': 1.3}):
        a = hal_config(st_(cal=False, digital=('D0',), **kw))
        b = hal_config(st_(cal=True, digital=('D0',), **kw))
        assert a[0]['analog'] == b[0]['analog'] and a[0]['threshold_uv'] == b[0]['threshold_uv']
        sa, sb = a[1]['A0'], b[1]['A0']
        assert sb['zero_code'] == sa['zero_code'] and sb['volts_per_code'] == sa['volts_per_code']
        assert b[0]['cal']['neutral'] is True


def test_corrected_scale_formula_and_inverse_trigger():
    install(table())
    st = st_(offset=2.0, trig_source='A0', trig_mode='normal', trig_level=2.8)
    hal, scale, w, _ = hal_config(st)
    s = scale['A0']
    c = s['cal']
    assert c['applied'] and c['mode'] == '1' and c['gain'] == 1.04
    zero = c['zero_code_raw']
    vpc = c['volts_per_code_raw']
    od = (128 - zero) / calib.CODES_PER_DIV
    assert abs(c['offset_div'] - od) < 1e-6
    assert s['zero_code'] == pytest.approx(zero - 0.8 - 0.2 * od)
    assert s['volts_per_code'] == pytest.approx(vpc / 1.04)
    # a code converts as ((code - zero) - (zl + sl*od)) / gain * vpc
    code = 200
    v = (code - s['zero_code']) * s['volts_per_code']
    assert v == pytest.approx(((code - zero) - (-0.8 - 0.2 * od)) / 1.04 * vpc)
    # the trigger code is the inverse map of the level, and the register zero stays the raw DAC code
    ana = hal['analog'][0]
    assert ana['voff_code'] == zero
    assert ana['trig_level_code'] == int(round(calib.volts_to_code(s, 2.8)))
    assert ana['trig_level_code'] - int(round(zero + 2.8 / vpc)) == 2      # 1.04 x the span, minus the zero shift


def test_mode_selection_and_fallbacks():
    install(table())
    assert hal_config(st_())[1]['A0']['cal']['mode'] == '1'
    assert hal_config(st_(analog=('A0', 'A1')))[1]['A0']['cal']['mode'] == '2'
    assert hal_config(st_(analog=('A0', 'A1', 'A2')))[1]['A0']['cal']['mode'] == '34'
    d = table()
    del d['analog']['1']['A0']['1']                     # no one-channel entry: the 3/4-channel one, with a note
    install(d)
    hal, scale, w, _ = hal_config(st_())
    assert scale['A0']['cal']['mode'] == '34' and 'used the 3/4-channel' in scale['A0']['cal']['note']
    assert any('calibration:' in x for x in w)
    del d['analog']['34']['A0']['1']                    # nothing at all: uncorrected, said so
    install(d)
    hal, scale, w, _ = hal_config(st_())
    assert scale['A0']['cal'] == {'applied': False, 'reason': 'no entry for A0 at 1 V/div: uncorrected'}
    assert isinstance(scale['A0']['zero_code'], int)


def test_inverted_channel_stays_uncorrected():
    install(table())
    hal, scale, w, _ = hal_config(st_(invert=['A0']))
    assert scale['A0']['cal']['applied'] is False and 'inverted' in scale['A0']['cal']['reason']


def test_threshold_is_programmed_for_the_target():
    install(table())
    hal, scale, w, _ = hal_config(st_(analog=(), digital=('D0',), threshold=1.4))
    want = (1.4 - 0.030) / 0.949
    assert hal['threshold_uv'] == int(round(want * 1e6))
    assert hal['cal']['threshold'] == {'target_v': 1.4, 'programmed_v': hal['threshold_uv'] / 1e6}
    hal, _, _, _ = hal_config(st_(analog=(), digital=('D0',), threshold=5.0))
    assert hal['threshold_uv'] == 5_000_000                       # clamped to 0..5 V


def _capture(st, codes):
    hal, scale, w, _ = hal_config(st)
    meta = {'channels': {c: scale[c] for c in st.analog}, 'analog_rate_hz': 1e6, 'digital_rate_hz': None,
            't0_analog_s': 0.0, 't0_digital_s': 0.0, 'applied': {}, 'acq': {}}
    if hal.get('cal'):
        meta['cal'] = scopemod.cal_record(hal, scale, st.analog)
    return Capture(meta, analog={c: codes for c in st.analog})


def test_capture_volts_clip_and_saved_round_trip(tmp_path):
    install(table())
    codes = np.array([100, 150, 200, CODE_MAX, CODE_MAX], dtype=np.uint8)
    cap = _capture(st_(offset=1.0), codes)
    s = cap.scale('A0')
    c = s['cal']
    want = ((codes.astype(np.float64) - c['zero_code_raw']) - (c['zero_lsb'] + c['offset_slope_lsb_per_div'] * c['offset_div'])) \
        / c['gain'] * c['volts_per_code_raw']
    assert np.allclose(cap.volts('A0'), want, atol=1e-5)
    assert cap.measure('A0')['clip_hi'] is True                   # clip detection follows the corrected map
    assert cap.calibration()['enabled'] is True
    p = str(tmp_path / 'c.npz')
    cap.save(p)
    calib.save(calib.placeholder())                               # the file in force changes: the capture does not
    back = load_capture(p)
    assert np.allclose(back.volts('A0'), cap.volts('A0')) and back.meta['cal']['analog']['A0']['gain'] == 1.04


def test_old_capture_without_cal_reads_uncorrected():
    st = st_(cal=False)
    cap = _capture(st, np.array([128, 160], dtype=np.uint8))
    assert cap.calibration() == {'enabled': False}
    assert cap.volts('A0')[0] == 0.0


def test_cal_without_a_file_is_refused():
    with pytest.raises(DscopeError) as e:
        hal_config(st_())
    assert e.value.code == 'CAL_MISSING'


# ------------------------------------------------------------------ generator
class FakeT:
    def __init__(self):
        self.w = []

    def write(self, reg, val):
        self.w.append((reg, val))


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(scopemod, 'STATE', str(tmp_path / 'state'))       # awg.json lands here, not in the checkout
    s = scopemod.Scope()
    s.dev = SimpleNamespace(t=FakeT(), zero_info=bytes(512))
    return s


def _levels(t):
    from openm454 import awg
    out, addr = {}, None
    for reg, val in t.w:
        if reg == awg.REG_ADDR:
            addr = val
        elif addr is not None:
            out[addr] = val
    return out.get(awg.A_HIGH), out.get(awg.A_LOW)


def test_awg_without_cal_is_unchanged(scope):
    install(table())
    st = scope.awg_set('sine', 1000.0, 2.5, 0.5)
    assert 'cal' not in st
    from openm454.awg_modes import level_code
    assert _levels(scope.dev.t) == (level_code(2.5), level_code(0.5))


def test_awg_cal_programs_for_the_wanted_output(scope):
    install(table())
    st = scope.awg_set('sine', 1000.0, 2.5, 0.5, cal=True)
    from openm454.awg_modes import level_code
    ph, pl = (2.5 - 0.00045) / 0.9896, (0.5 - 0.00045) / 0.9896
    assert _levels(scope.dev.t) == (level_code(ph), level_code(pl))
    assert st['cal']['programmed_v'] == {'high': round(ph, 5), 'low': round(pl, 5)}
    assert abs(st['high_v'] - 2.5) < 0.01 and abs(st['low_v'] - 0.5) < 0.01     # expected output (quantized)
    assert 'clamped' not in st['cal']


def test_awg_cal_clamps_to_the_programming_limit(scope):
    install(table())
    st = scope.awg_set('square', 1000.0, 3.0, 0.0, cal=True)
    assert st['cal']['programmed_v']['high'] == 3.0 and st['cal']['clamped'] == ['high']   # 0 V low: 0.45 mV, < half a step
    assert 'calibrated output range' in st['cal']['warning']
    assert st['high_v'] < 3.0                                     # 0.9896 * 3 V: the generator cannot reach 3 V
    st = scope.awg_dc(1.0, cal=True)
    assert st['cal']['programmed_v']['level'] == round((1.0 - 0.00045) / 0.9896, 5)
    assert abs(st['level_v'] - 1.0) < 0.01


def test_awg_cal_refuses_another_unit(scope):
    install(dict(table(), unit_id='0' * 16))
    with pytest.raises(DscopeError) as e:
        scope.awg_dc(1.0, cal=True)
    assert e.value.code == 'CAL_MISMATCH'
    assert scope.dev.t.w == []                                     # nothing was sent


# ------------------------------------------------------------------ log, flags, CLI, MCP
def test_loop_mode_uses_the_three_four_channel_table():
    install(table())
    hal, scale, w = logger.loop_hal(st_(), 1e6)
    assert scale['A0']['cal']['mode'] == '34' and scale['A0']['cal']['gain'] == 1.03
    hal, scale, w = logger.loop_hal(st_(), 200e6, fold=True)
    assert scale['A0']['cal']['mode'] == '1'


def test_vertical_flags_keep_cal():
    assert '--cal' in scopemod.vertical_flags(st_())
    assert '--cal' not in scopemod.vertical_flags(st_(cal=False))


def run_cli(argv, capsys):
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_template_import_status(tmp_path, capsys):
    f = str(tmp_path / 'my.json')
    code, d = run_cli(['cal', 'template', '--out', f], capsys)
    assert code == 0 and json.load(open(f)) == calib.placeholder()
    code, d = run_cli(['cal', 'template', '--out', f], capsys)
    assert code == 2 and d['error']['code'] == 'INVALID_ARG'          # no overwrite without --force
    code, d = run_cli(['cal'], capsys)
    assert code == 0 and d['exists'] is False
    json.dump(table(), open(f, 'w'))
    code, d = run_cli(['cal', 'import', f], capsys)
    assert code == 0 and d['neutral'] is False and os.path.exists(calib.PATH)
    code, d = run_cli(['cal', 'status'], capsys)
    assert d['exists'] and d['awg']['gain'] == 0.9896 and d['coverage']['1']['non_neutral'] == 1
    bad = table()
    bad['threshold']['gain'] = 3
    json.dump(bad, open(f, 'w'))
    code, d = run_cli(['cal', 'import', f], capsys)
    assert code == 2 and d['error']['code'] == 'CAL_INVALID'


def test_cli_debug_plan_with_cal(capsys):
    install(table())
    code, d = run_cli(['debug', 'plan', '-c', 'A0,D0', '--vdiv', '1V', '--cal', '--duration', '1ms'], capsys)
    assert code == 0 and d['hal']['cal']['mode'] == '1'
    code, d = run_cli(['debug', 'plan', '-c', 'A0,D0', '--vdiv', '1V', '--duration', '1ms'], capsys)
    assert 'cal' not in d['hal']


def test_mcp_mapping_and_summary():
    install(calib.placeholder())                                  # a neutral file: nothing to apply by default
    assert '--cal' in mcp_server.capture_args({'channels': 'A0', 'cal': True})
    assert '--cal' not in mcp_server.capture_args({'channels': 'A0'})
    assert mcp_server.awg_args({'action': 'dc', 'level': '1V', 'cal': True})[-1] == '--cal'
    assert mcp_server.awg_args({'action': 'set', 'wave': 'sine', 'cal': True})[-1] == '--cal'
    assert '--cal' in mcp_server.record_args({'mode': 'log', 'channels': 'A0', 'cal': True})
    doc = {'ok': True, 'cmd': 'capture', 'applied': {'channels': ['A0'], 'threshold_v': 1.4, 'threshold_programmed_v': 1.44,
                                                     'cal': {'enabled': True}}, 'cal': {'enabled': True}}
    out = mcp_server.summarize(doc)
    assert out['applied']['cal'] == {'enabled': True} and out['applied']['threshold_programmed_v'] == 1.44
    assert out['cal'] == {'enabled': True}


# ------------------------------------------------------------------ review fixes
def test_log_cal_refuses_another_unit_before_anything_runs(capsys, tmp_path, monkeypatch):
    import types
    from openm454 import host
    from tests.support import logging as tl
    monkeypatch.setattr(host, 'HOST_FILE', str(tmp_path / 'host.json'))
    monkeypatch.setattr(cli, 'STOP_REQUEST', str(tmp_path / 'stop.request'))
    monkeypatch.setattr(calib, 'unit_id', lambda zi: 'f' * 16)    # this "scope" is unit ffff..., independent of state/
    install(dict(table(), unit_id='0' * 16))
    args = cli.build_parser().parse_args(['log', '-c', 'A0,D0', '--vdiv', '1V', '--rate', '1M', '--duration', '0.05s',
                                          '--no-save', '--cal'])
    st, pl = cli.log_plan(args, scopemod._cached_zero(), defer=True)
    layout, an, dg, stream = tl.fake_signal(pl, 0.1)

    class Bound(tl.FakeScope):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.dev = types.SimpleNamespace(zero_info=scopemod._cached_zero())

        def _check_link(self):
            raise AssertionError('the scope was used before the unit check')

    with pytest.raises(DscopeError) as e:
        cli.cmd_log(args, Bound(stream, pl['packet_bytes']))
    assert e.value.code == 'CAL_MISMATCH'


@pytest.mark.parametrize('probe, vdiv, offset, level, code', [
    (10.0, 0.01, 0.0, -0.040, 11), (10.0, 0.01, 0.5, 0.040, 117), (10.0, 0.05, 0.5, 0.040, 24)])
def test_trigger_codes_without_cal_are_the_old_ones(probe, vdiv, offset, level, code):
    """Half-code ties: the arithmetic must stay the pre-calibration one (register programs byte-identical)."""
    st = st_(cal=False, trig_source='A0', trig_mode='normal', trig_level=level, vdiv=vdiv, offset=offset)
    st.probe = {c: probe for c in st.probe}
    hal, scale, w, _ = hal_config(st)
    assert hal['analog'][0]['trig_level_code'] == code


def test_awg_cal_refuses_what_is_refused_without_it(scope):
    install(table())
    for hi, lo in ((3.5, 0.0), (2.0, -0.1)):
        with pytest.raises(DscopeError) as e:
            scope.awg_set('sine', 1000.0, hi, lo, cal=True)
        assert e.value.code == 'INVALID_ARG'
    with pytest.raises(DscopeError) as e:
        scope.awg_dc(3.2, cal=True)
    assert e.value.code == 'INVALID_ARG'
    assert scope.dev.t.w == []


def test_awg_cal_levels_clamped_together_are_explained(scope):
    d = table()
    d['awg'] = {'gain': 0.95, 'offset_v': 0.0}                    # 2.9 and 3.0 V both need more than 3 V
    install(d)
    with pytest.raises(DscopeError) as e:
        scope.awg_set('sine', 1000.0, 3.0, 2.9, cal=True)
    assert e.value.code == 'INVALID_ARG' and 'calibrated output range' in e.value.message


def test_threshold_clamp_is_reported():
    install(table())
    hal, scale, w, _ = hal_config(st_(analog=(), digital=('D0',), threshold=5.0))
    t = hal['cal']['threshold']
    assert t['clamped'] and t['requested_v'] == 5.0 and t['programmed_v'] == 5.0
    assert t['target_v'] == pytest.approx(0.949 * 5.0 + 0.030)
    assert any('outside the calibrated range' in x for x in w)


def test_awg_record_reaches_the_result():
    res = {'warnings': ['x']}
    cli._awg_into(res, {'high_v': 2.9, 'cal': {'warning': 'calibrated output range is ...'}})
    assert res['awg']['high_v'] == 2.9 and res['warnings'] == ['x', 'calibrated output range is ...']
    res = {}
    cli._awg_into(res, {'high_v': 2.9})                           # without --cal: nothing added
    assert res == {}


def test_frequency_fit_rising_edges_only():
    from openm454 import measure
    rate = 1e6
    # 80.8 samples per period, 50 periods, edges faster than a sample: the fit beats first-to-last
    n = int(50 * 80.8) + 40
    t = np.arange(n)
    x = np.where(((t + 0.37) / 80.8) % 1.0 < 0.5, 3.0, 0.0)
    m = measure.analog(x, rate)
    f_true = rate / 80.8
    assert abs(m['freq_hz'] / f_true - 1) < 0.5 * m['freq_res_hz'] / f_true
    # a duty cycle drifting from 30 to 70 %: the rising edges keep the period (falls are not mixed in)
    period = 200.0
    k = np.arange(int(40 * period))
    duty = 0.3 + 0.4 * k / k.size
    y = np.where((k / period) % 1.0 < duty, 3.0, 0.0)
    m = measure.analog(y, rate)
    assert abs(m['freq_hz'] / (rate / period) - 1) < 1e-4


def test_no_resolution_figure_on_the_glitch_path():
    from openm454 import measure
    x = np.tile(np.r_[np.zeros(50), np.full(50, 3.0)], 30)
    x[1020:1023] = 3.0                                             # a glitch: irregular intervals
    x[2520:2522] = 3.0
    m = measure.analog(x, 1e6)
    if m.get('period_irregular'):
        assert 'freq_res_hz' not in m


# ------------------------------------------------------------------ the 20 MHz limit (measured 2026-11-25)
def test_bw20_gain_is_folded_in_only_with_the_limit_on_and_per_channel_count_mode():
    install(table(bw20_gain={'1': 0.978, '2': 0.989, '34': 0.994}))
    assert not calib.neutral(calib.active())
    off = hal_config(st_())[1]['A0']
    on = hal_config(st_(bw20=['A0']))[1]['A0']
    assert 'bw20_gain' not in off['cal'] and off['cal']['gain'] == 1.04
    assert on['cal']['bw20_gain'] == 0.978 and on['cal']['gain'] == pytest.approx(1.04 * 0.978)
    assert on['volts_per_code'] == pytest.approx(off['volts_per_code'] / 0.978)
    assert on['zero_code'] == off['zero_code']                       # the factor is a gain, the zero stays
    assert 'not corrected' not in on['cal'].get('note', '')
    two = hal_config(st_(analog=('A0', 'A1'), bw20=['A0']))[1]
    assert two['A0']['cal']['gain'] == pytest.approx(1.02 * 0.989)   # the two-channel factor with the two-channel entry
    assert 'bw20_gain' not in two['A1']['cal']                       # A1 has the limit off
    four = hal_config(st_(analog=('A0', 'A1', 'A2', 'A3'), bw20=['A0']))[1]['A0']
    assert four['cal']['gain'] == pytest.approx(1.03 * 0.994)


def test_a_table_without_bw20_gain_says_that_the_limit_is_not_corrected():
    d = table()
    del d['bw20_gain']                                               # a table written before 2026-11-25
    install(d)
    assert calib.validate(d) == [] and calib.bw20_gain(d, '1') is None
    on = hal_config(st_(bw20=['A0']))[1]['A0']
    assert on['cal']['gain'] == 1.04 and 'bw20_gain' not in on['cal']
    assert 'not corrected' in on['cal']['note'] and '20 MHz limit' in on['cal']['note']


def test_the_placeholder_factor_of_one_changes_nothing():
    install(calib.placeholder())
    a = hal_config(st_(cal=False, bw20=['A0']))[1]['A0']
    b = hal_config(st_(cal=True, bw20=['A0']))[1]['A0']
    assert b['volts_per_code'] == a['volts_per_code'] and b['zero_code'] == a['zero_code']


@pytest.mark.parametrize('analog, word', [
    (('A0',), 'about 16.5 MHz'),
    (('A0', 'A1'), 'about 24.5 MHz'),
    (('A0', 'A1', 'A2'), 'above 30 MHz'),
    (('A0', 'A1', 'A2', 'A3'), 'above 30 MHz'),
])
def test_the_limit_comes_with_a_hint_about_its_real_corner(analog, word):
    hints = hal_config(st_(analog=analog, cal=False, bw20=['A0']))[3]
    assert len([h for h in hints if '20 MHz limit' in h]) == 1
    h = next(h for h in hints if '20 MHz limit' in h)
    assert word in h and '%d analog channel' % len(analog) in h
    assert not [h for h in hal_config(st_(analog=analog, cal=False))[3] if '20 MHz limit' in h]
    # the limit on a channel that is not recorded says nothing
    others = [c for c in ('A0', 'A1', 'A2', 'A3') if c not in analog]
    if others:
        assert not [h for h in hal_config(st_(analog=analog, cal=False, bw20=others))[3] if '20 MHz limit' in h]


# ------------------------------------------------------------------ an installed table is the default (MCP) / hinted (CLI)
def test_installed_is_quiet_and_only_true_for_a_table_that_corrects_something(tmp_path):
    assert calib.installed(str(tmp_path / 'none.json')) is None                 # missing: no error
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json', encoding='utf-8')
    assert calib.installed(str(bad)) is None                                     # unreadable: no error
    install(calib.placeholder())
    assert calib.installed() is None                                             # neutral corrects nothing
    install(table())
    assert calib.installed()['awg']['gain'] == 0.9896


def test_mcp_applies_an_installed_table_unless_told_not_to():
    install(table())
    assert '--cal' in mcp_server.capture_args({'channels': 'A0'})
    assert '--cal' not in mcp_server.capture_args({'channels': 'A0', 'cal': False})
    assert '--cal' in mcp_server.capture_args({'channels': 'A0', 'cal': True})
    assert mcp_server.awg_args({'action': 'dc', 'level': '1V'})[-1] == '--cal'
    assert mcp_server.awg_args({'action': 'set', 'wave': 'sine'})[-1] == '--cal'
    assert '--cal' not in mcp_server.awg_args({'action': 'set', 'wave': 'sine', 'cal': False})
    assert '--cal' not in mcp_server.awg_args({'action': 'off'})                 # off / status take no options
    assert '--cal' in mcp_server.record_args({'mode': 'log', 'channels': 'A0'})
    assert '--cal' not in mcp_server.record_args({'mode': 'log', 'channels': 'A0', 'cal': False})
    install(calib.placeholder())
    assert '--cal' not in mcp_server.capture_args({'channels': 'A0'})
    assert '--cal' not in mcp_server.awg_args({'action': 'dc', 'level': '1V'})
    assert 'cal.installed' in mcp_server.INSTRUCTIONS


def test_a_capture_without_cal_says_when_a_table_was_left_unused():
    from openm454.scope import CAL_UNUSED_HINT
    install(table())
    assert CAL_UNUSED_HINT in hal_config(st_(cal=False))[3]
    assert CAL_UNUSED_HINT not in hal_config(st_(cal=True))[3]
    assert CAL_UNUSED_HINT not in hal_config(st_(analog=(), digital=('D0',), cal=False))[3]     # no volts to correct
    install(calib.placeholder())
    assert CAL_UNUSED_HINT not in hal_config(st_(cal=False))[3]

