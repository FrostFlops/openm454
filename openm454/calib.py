"""Opt-in calibration: PC-side corrections measured against a reference meter (never written to the device).

The device's own EEPROM calibration ("zero info") sets the offset DACs; the corrections here act on top of it, on
the PC, and only when asked for (CLI --cal, MCP cal: true). One JSON file per unit, in the data folder:
state/calibration.json (git-ignored). The package ships a neutral placeholder (gain 1, every offset 0) with the same
structure, which `openm454 cal template` writes out for filling in.

  analog    per channel-count mode ('1' one analog channel, '2' two, '34' three or four), channel and hardware
            V/div (the V/div at the BNC, '%g' of volts: '0.001' .. '5'):
                code - zero_code = gain * V / volts_per_code + zero_lsb + offset_slope_lsb_per_div * offset_div
            so V = ((code - zero_code) - (zero_lsb + offset_slope_lsb_per_div * offset_div)) / gain * volts_per_code
            (x probe), offset_div = the offset in divisions. Per capture this is affine in the code: the scale entry
            stores the effective zero_code and volts_per_code, so every reader of the scale is corrected (saved files
            carry it and reconvert with their own correction).
  awg       output = gain * programmed + offset_v: a requested level v is programmed as (v - offset_v) / gain,
            clamped to the generator's 0..3 V.
  threshold switching voltage = gain * programmed + offset_v (one threshold DAC for all eight digital inputs, so
            per-input offsets cannot be corrected).

A file may name the unit it was measured on (unit_id = sha256 of the 512-byte EEPROM zero-info block, the first 16
hex digits); a mismatch is refused. This module has no numpy and no device access.
"""
import copy
import hashlib
import json
import os

from .errors import DscopeError
from .paths import ROOT

PATH = os.path.join(ROOT, 'state', 'calibration.json')
FORMAT = 'openm454-calibration'
VERSION = 1
MODES = ('1', '2', '34')
CHANNELS = ('A0', 'A1', 'A2', 'A3')
VDIV_KEYS = ('0.001', '0.002', '0.005', '0.01', '0.02', '0.05', '0.1', '0.2', '0.5', '1', '2', '5')
CODES_PER_DIV = 235.0 / 8.0
# sanity bounds: a typo must be refused, not applied
ANALOG_GAIN = (0.8, 1.25)
ANALOG_ZERO_LSB = 30.0
ANALOG_SLOPE = 2.0
BW20_GAIN = (0.9, 1.02)          # optional bw20_gain: the 20 MHz limit's own factor on the gain, per mode
AWG_GAIN = (0.9, 1.1)
AWG_OFFSET_V = 0.1
THR_GAIN = (0.8, 1.2)
THR_OFFSET_V = 0.3
AWG_V_MAX = 3.0
AWG_CLAMP_TOL = 0.5 / (1023 * 0.91525 / 6.0)    # half a generator step (~3.2 mV): smaller clamps are not reported
THR_V_MAX = 5.0

NEUTRAL_ENTRY = {'gain': 1.0, 'zero_lsb': 0.0, 'offset_slope_lsb_per_div': 0.0}

_cache = {}


def placeholder():
    """The neutral table: every mode, channel and V/div present, gain 1, offsets 0 (an exact identity)."""
    return {'format': FORMAT, 'version': VERSION, 'unit_id': None, 'created': None,
            'note': 'neutral placeholder: gain 1 and every offset 0 (no correction); replace the entries with values '
                    'measured on your unit against a reference meter',
            'analog': {m: {c: {v: dict(NEUTRAL_ENTRY) for v in VDIV_KEYS} for c in CHANNELS} for m in MODES},
            'bw20_gain': {m: 1.0 for m in MODES},
            'awg': {'gain': 1.0, 'offset_v': 0.0},
            'threshold': {'gain': 1.0, 'offset_v': 0.0}}


def vdiv_key(vdiv_v):
    """'%g' key of a hardware V/div in volts (0.1 -> '0.1', 1.0 -> '1')."""
    return '%g' % float('%.6g' % vdiv_v)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float('inf')


def validate(doc):
    """Problems with a calibration document (empty list = usable)."""
    p = []
    if not isinstance(doc, dict):
        return ['not a JSON object']
    if doc.get('format') != FORMAT:
        p.append("format must be '%s'" % FORMAT)
    if doc.get('version') != VERSION:
        p.append('version must be %d' % VERSION)
    uid = doc.get('unit_id')
    if uid is not None and not (isinstance(uid, str) and len(uid) == 16 and all(c in '0123456789abcdef' for c in uid)):
        p.append('unit_id must be null or 16 lower-case hex digits')
    an = doc.get('analog', {})
    if not isinstance(an, dict):
        p.append('analog must be an object')
        an = {}
    for m, chans in an.items():
        if m not in MODES:
            p.append('analog mode %r unknown (use %s)' % (m, ', '.join(MODES)))
            continue
        if not isinstance(chans, dict):
            p.append('analog.%s must be an object' % m)
            continue
        for c, vd in chans.items():
            if c not in CHANNELS:
                p.append('analog.%s: channel %r unknown' % (m, c))
                continue
            if not isinstance(vd, dict):
                p.append('analog.%s.%s must be an object' % (m, c))
                continue
            for k, e in vd.items():
                where = 'analog.%s.%s.%s' % (m, c, k)
                if k not in VDIV_KEYS:
                    p.append('%s: V/div key unknown (use %s)' % (where, ', '.join(VDIV_KEYS)))
                    continue
                if not isinstance(e, dict):
                    p.append('%s must be an object' % where)
                    continue
                g, z, s = e.get('gain'), e.get('zero_lsb', 0.0), e.get('offset_slope_lsb_per_div', 0.0)
                if not _num(g) or not ANALOG_GAIN[0] <= g <= ANALOG_GAIN[1]:
                    p.append('%s: gain must be %g .. %g' % (where, ANALOG_GAIN[0], ANALOG_GAIN[1]))
                if not _num(z) or abs(z) > ANALOG_ZERO_LSB:
                    p.append('%s: zero_lsb must be within +-%g' % (where, ANALOG_ZERO_LSB))
                if not _num(s) or abs(s) > ANALOG_SLOPE:
                    p.append('%s: offset_slope_lsb_per_div must be within +-%g' % (where, ANALOG_SLOPE))
    bw = doc.get('bw20_gain')
    if bw is not None:
        if not isinstance(bw, dict):
            p.append('bw20_gain must be an object')
        else:
            for m, g in bw.items():
                if m not in MODES:
                    p.append('bw20_gain mode %r unknown (use %s)' % (m, ', '.join(MODES)))
                elif not _num(g) or not BW20_GAIN[0] <= g <= BW20_GAIN[1]:
                    p.append('bw20_gain.%s must be %g .. %g' % (m, BW20_GAIN[0], BW20_GAIN[1]))
    for key, gb, ob in (('awg', AWG_GAIN, AWG_OFFSET_V), ('threshold', THR_GAIN, THR_OFFSET_V)):
        e = doc.get(key)
        if e is None:
            continue
        if not isinstance(e, dict):
            p.append('%s must be an object' % key)
            continue
        if not _num(e.get('gain')) or not gb[0] <= e['gain'] <= gb[1]:
            p.append('%s.gain must be %g .. %g' % (key, gb[0], gb[1]))
        if not _num(e.get('offset_v', 0.0)) or abs(e.get('offset_v', 0.0)) > ob:
            p.append('%s.offset_v must be within +-%g V' % (key, ob))
    return p


def read(path):
    """Read and validate a calibration file -> document (CAL_MISSING / CAL_INVALID)."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            doc = json.load(f)
    except FileNotFoundError:
        raise DscopeError('CAL_MISSING', 'no calibration file at %s' % path.replace('\\', '/'),
                          "make one with 'openm454 cal template --out FILE', fill in your unit's values, then "
                          "'openm454 cal import FILE'; or leave out --cal")
    except (OSError, ValueError) as e:
        raise DscopeError('CAL_INVALID', 'calibration file %s unreadable: %s' % (path.replace('\\', '/'), e),
                          "fix the JSON or re-import it with 'openm454 cal import FILE'")
    probs = validate(doc)
    if probs:
        raise DscopeError('CAL_INVALID', 'calibration file %s: %s' % (path.replace('\\', '/'), '; '.join(probs[:5])),
                          "fix the values ('openm454 cal template' shows the layout)")
    return doc


def active(path=None):
    """The calibration in force (state/calibration.json), cached per process and file modification time."""
    path = path or PATH
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = None
    if key is not None and key in _cache:
        return _cache[key]
    doc = read(path)
    if key is not None:
        _cache.clear()
        _cache[key] = doc
    return doc


def installed(path=None):
    """The calibration in force when it is usable and corrects something, else None. Never raises: a missing,
    unreadable, invalid or neutral file is 'no calibration'. For defaults and hints (the MCP server applies an
    installed table unless told not to); --cal itself still reports CAL_MISSING / CAL_INVALID."""
    try:
        doc = active(path)
    except DscopeError:
        return None
    return None if neutral(doc) else doc


def save(doc, path=None):
    """Write atomically (validated first)."""
    path = path or PATH
    probs = validate(doc)
    if probs:
        raise DscopeError('CAL_INVALID', 'calibration not saved: %s' % '; '.join(probs[:5]))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    _cache.clear()
    return path


def unit_id(zero_info):
    """Identity of a unit: sha256 of its EEPROM zero-info block (not secret; the security key is never used)."""
    if not isinstance(zero_info, (bytes, bytearray, memoryview)) or not len(zero_info):
        return None                      # no raw block (e.g. a parsed default offline): the unit is unknown
    return hashlib.sha256(bytes(zero_info)).hexdigest()[:16]


def check_unit(doc, zero_info):
    """Refuse a file measured on another unit (a file without unit_id applies to any unit)."""
    want = doc.get('unit_id')
    have = unit_id(zero_info)
    if want and have and want != have:
        raise DscopeError('CAL_MISMATCH', 'the calibration file belongs to unit %s, this scope is %s' % (want, have),
                          "import this unit's own table ('openm454 cal import FILE --bind'), or leave out --cal")


def neutral(doc):
    """True when nothing in the document corrects anything (the placeholder)."""
    for chans in doc.get('analog', {}).values():
        for vd in chans.values():
            for e in vd.values():
                if (e.get('gain', 1.0), e.get('zero_lsb', 0.0), e.get('offset_slope_lsb_per_div', 0.0)) != (1.0, 0.0, 0.0):
                    return False
    if any(g != 1.0 for g in (doc.get('bw20_gain') or {}).values()):
        return False
    for key in ('awg', 'threshold'):
        e = doc.get(key) or {}
        if (e.get('gain', 1.0), e.get('offset_v', 0.0)) != (1.0, 0.0):
            return False
    return True


def summary(doc, path=None):
    """Short description for metadata and `cal status`."""
    path = (path or PATH).replace('\\', '/')
    return {'enabled': True, 'file': os.path.basename(path), 'unit_id': doc.get('unit_id'),
            'created': doc.get('created'), 'neutral': neutral(doc)}


def mode_key(n_analog):
    """Table mode for a number of enabled analog channels: the converters are shared differently (interleave)."""
    return '1' if n_analog == 1 else '2' if n_analog == 2 else '34'


def analog_entry(doc, mode, ch, vdiv_v):
    """(entry, mode used, reason): the table entry for (mode, channel, hardware V/div). A one- or two-channel mode
    without the entry falls back to the three/four-channel one (reason says so); none at all -> (None, None, reason)."""
    k = vdiv_key(vdiv_v)
    an = doc.get('analog') or {}
    e = ((an.get(mode) or {}).get(ch) or {}).get(k)
    if e is not None:
        return e, mode, None
    if mode != '34':
        e = ((an.get('34') or {}).get(ch) or {}).get(k)
        if e is not None:
            return e, '34', 'no %s-channel entry for %s at %s V/div: used the 3/4-channel one' % (mode, ch, k)
    return None, None, 'no entry for %s at %s V/div: uncorrected' % (ch, k)


def bw20_gain(doc, mode):
    """The factor by which the 20 MHz limit changes a channel's gain in this channel-count mode, or None when the
    table does not say. The limit lowers the gain at every frequency, DC included, by an amount that depends on
    how many analog channels are on (measured 2026-11-25 on one unit: 0.978 with one analog channel, 0.989 with
    two, 0.994 with three or four)."""
    g = (doc.get('bw20_gain') or {}).get(mode)
    return float(g) if _num(g) else None


def corrected_scale(base, entry, mode, reason=None, coupling='dc', bw20=False, bw20_gain=None):
    """The scale entry with the correction folded in. base: {'zero_code' (the integer offset DAC zero),
    'vdiv_v', 'volts_per_code', 'probe'}; the effective zero_code / volts_per_code make every reader corrected and
    the raw values stay in 'cal'. bw20_gain: the table's factor for the 20 MHz limit in the channel-count mode in
    use (applied only when bw20 is on)."""
    z, vpc = float(base['zero_code']), float(base['volts_per_code'])
    od = (128.0 - z) / CODES_PER_DIV
    g = float(entry['gain'])
    fg = float(bw20_gain) if (bw20 and bw20_gain) else None
    if fg:
        g *= fg
    zl = float(entry.get('zero_lsb', 0.0))
    sl = float(entry.get('offset_slope_lsb_per_div', 0.0))
    s = dict(base)
    s['zero_code'] = z + zl + sl * od
    s['volts_per_code'] = vpc / g
    s['cal'] = {'applied': True, 'mode': mode, 'gain': g, 'zero_lsb': zl, 'offset_slope_lsb_per_div': sl,
                'offset_div': round(od, 6), 'zero_code_raw': base['zero_code'], 'volts_per_code_raw': vpc}
    if fg:
        s['cal']['bw20_gain'] = fg                  # already inside 'gain'
    if reason:
        s['cal']['note'] = reason
    if coupling != 'dc' or bw20:
        s['cal']['note'] = ((s['cal'].get('note') + '; ') if s['cal'].get('note') else '') + \
            'measured with DC coupling and full bandwidth (AC/BW20 shift the zero by up to about 1 LSB)'
        if bw20 and not fg:
            s['cal']['note'] += ('; the 20 MHz limit lowers the gain by about 0.6 % (3-4 analog channels) to 2.2 % '
                                 '(one channel): not corrected, the table has no bw20_gain for this mode')
    return s


def uncorrected_scale(base, reason):
    s = dict(base)
    s['cal'] = {'applied': False, 'reason': reason}
    return s


def volts_to_code(s, v):
    """Inverse of the (corrected) volts conversion: volts at the probe tip -> ADC code (float)."""
    return s['zero_code'] + v / (s['volts_per_code'] * s.get('probe', 1.0))


def awg_program(doc, v):
    """(level to program, clamped?) for a requested output level v."""
    e = doc.get('awg') or {}
    p = (v - float(e.get('offset_v', 0.0))) / float(e.get('gain', 1.0))
    q = min(max(p, 0.0), AWG_V_MAX)
    return q, abs(q - p) > AWG_CLAMP_TOL


def awg_output(doc, programmed):
    """Expected output for a programmed level (after the generator's own quantization)."""
    e = doc.get('awg') or {}
    return float(e.get('gain', 1.0)) * programmed + float(e.get('offset_v', 0.0))


def awg_range(doc):
    """(lowest, highest) output reachable inside the 0..3 V programming limit."""
    return awg_output(doc, 0.0), awg_output(doc, AWG_V_MAX)


def threshold_program(doc, target):
    """(threshold voltage to program, clamped?) for a wanted switching voltage; programmed within 0..5 V."""
    e = doc.get('threshold') or {}
    p = (target - float(e.get('offset_v', 0.0))) / float(e.get('gain', 1.0))
    q = min(max(p, 0.0), THR_V_MAX)
    return q, abs(q - p) > 1e-9


def threshold_output(doc, programmed):
    """Expected switching voltage for a programmed threshold."""
    e = doc.get('threshold') or {}
    return float(e.get('gain', 1.0)) * programmed + float(e.get('offset_v', 0.0))


def threshold_range(doc):
    """(lowest, highest) switching voltage reachable inside the 0..5 V programming range."""
    return threshold_output(doc, 0.0), threshold_output(doc, THR_V_MAX)


def coverage(doc):
    """Entries present per mode (and how many differ from neutral)."""
    out = {}
    for m, chans in (doc.get('analog') or {}).items():
        n = nn = 0
        for vd in chans.values():
            for e in vd.values():
                n += 1
                nn += (e.get('gain', 1.0), e.get('zero_lsb', 0.0), e.get('offset_slope_lsb_per_div', 0.0)) != (1.0, 0.0, 0.0)
        out[m] = {'entries': n, 'non_neutral': nn}
    return out


def public(doc):
    """A copy for output (deep copy: callers must not change the cached document)."""
    return copy.deepcopy(doc)
