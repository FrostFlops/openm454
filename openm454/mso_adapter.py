"""Glue between scope.py (user-level settings) and the protocol modules mso.py (register compiler) and
packet.py (stream decoder)."""
import numpy as np

from . import mso
from .errors import DscopeError


def to_mso(hal):
    cfg = mso.MsoConfig()
    for i, a in enumerate(hal['analog']):
        ch = cfg.analog[i]
        ch.enable = bool(a['enable'])
        ch.coupling = a['coupling']
        ch.bw20 = bool(a['bw20'])
        ch.invert = bool(a['invert'])
        ch.vdiv_uv = int(a['vdiv_uv'])
        ch.voff_code = int(a['voff_code'])
        ch.trig_level_code = int(a['trig_level_code'])
    cfg.digital = [bool(x) for x in hal['digital']]
    cfg.threshold_uv = int(hal['threshold_uv'])
    cfg.sample_time_ps = int(hal['sample_time_ps'])
    cfg.storage_depth = int(hal['storage_depth'])
    cfg.auto_rate = True
    t = hal['trigger']
    cfg.trig_enable = bool(t['enable'])
    cfg.trig_mode = 1                       # normal: the host implements 'auto' (timeout -> forced capture)
    cfg.trig_channel = int(t['ch_index'])
    cfg.trig_channel_type = int(t['ch_type'])
    cfg.trig_type = int(t['type'])
    cfg.trig_pulse_ps = int(t['pulse_ps'])
    cfg.trig_margin = int(t['margin'])
    cfg.trig_hold_ps = int(t['hold_ps'])
    cfg.repeat_mode = bool(hal.get('repeat'))
    cfg.stream_always = bool(hal.get('stream_always'))
    cfg.loop_mode = False
    if hal.get('loop'):
        # loop mode (DSWave's Data Acquisition mode): endless stream, trigger off, one explicit rate for every
        # channel (AUTORATE off; :SAMPLEtime is ignored), fold 0 (100 MSa/s per channel) unless loop_fold
        cfg.loop_mode = True
        cfg.loop_fold = bool(hal.get('loop_fold'))   # interleave (rates above 100 MSa/s on 1-2 channels)
        cfg.auto_rate = False
        cfg.analog_rate = cfg.digital_rate = int(round(hal['rate']))
        cfg.divider = hal.get('divider')            # exact 100 MHz / N (rates that are not whole Hz, e.g. N = 3)
        cfg.trig_enable = False
        cfg.trig_mode = 0
    ar, ad, dr, dd = mso.rates_and_depths(cfg)
    depth = ad if cfg.trig_channel_type == 1 and any(a.enable for a in cfg.analog) else dd
    cfg.trig_sample_index = int(round(t['pos_pct'] / 100.0 * depth))
    if not cfg.trig_enable:
        cfg.trig_mode = 0                   # free-running (DSWave 'auto' with no trigger enabled)
    if cfg.trig_channel_type == 1 and not any(a.enable for a in cfg.analog) or \
            cfg.trig_channel_type == 0 and not any(cfg.digital):
        # the depth/pretrigger registers follow the trigger domain; one without channels gives a short HORI_DEPTH
        raise DscopeError('INVALID_ARG', 'the trigger channel type has no enabled channel',
                          'trigger on an enabled channel (or enable a channel of that type)')
    return cfg


def compile(hal, zero_info, include_cross=True):
    """-> (program, geometry dict). geometry['need_bytes'] includes the 4 KiB tail (DSWave only ends a frame once
    the tail has been parsed); 'record_s' is the time the device needs to acquire the record in the trigger domain;
    'small_pkt' marks the small-packet roll mode, which no recorded DSWave run used (callers refuse it)."""
    from . import packet
    cfg = to_mso(hal)
    try:
        g = mso.geometry(cfg)
        prog = mso.setup_program(cfg, zero_info, include_cross=include_cross)
    except ValueError as e:
        if 'hw_depth' in str(e):
            raise DscopeError('INVALID_ARG', 'the record does not fit the 4 Gbit sample memory (about 400M samples on '
                              '1 analog channel, 100M each on 4, 400M each on 8 digital channels at 100 MSa/s)',
                              'use fewer --samples, a lower --rate or a shorter --duration, or fewer channels') from e
        raise DscopeError('INVALID_ARG', 'settings out of the hardware range: %s' % e,
                          'shorten --duration/--timebase or raise --samples/--rate; shorten --holdoff or the pulse width') from e
    for addr, val in prog:
        if addr >= 0xF0 and addr not in (0xF9, 0xF4):
            raise DscopeError('UNSAFE_OPERATION', 'compiler produced MCU register write %02X' % addr)
    geom = {
        'analog_rate': g.analog_rate if g.n_analog else None,
        'digital_rate': g.digital_rate if g.n_digital else None,
        'analog_depth': g.analog_depth if g.n_analog else 0,
        'digital_depth': g.digital_depth if g.n_digital else 0,
        'pkt_bytes': g.transfer_size, 'transfer_count': g.transfer_count,
        'need_bytes': None if cfg.loop_mode else g.acqu_bytes + (0 if cfg.repeat_mode else packet.TAIL_BYTES),
        'acqu_bytes': g.acqu_bytes, 'loop': bool(cfg.loop_mode),
        'small_pkt': bool(g.small_pkt),
        'hw_mode': g.hw_mode, 'pretrig': g.pretrig, 'sample_index': cfg.trig_sample_index,
        'trig_domain': 'analog' if cfg.trig_channel_type == 1 and g.n_analog else 'digital',
        'afold_log2': g.afold_log2, 'dfold_log2': g.dfold_log2, 'send_samples': g.send_samples,
        'blk_bytes': g.blk_bytes, 'repeat': bool(cfg.repeat_mode), 'roll': bool(g.roll),
        '_regs': {a: v for a, v in prog},
    }
    dom = geom['trig_domain']
    geom['record_s'] = (geom[dom + '_depth'] / geom[dom + '_rate']) if geom[dom + '_rate'] and not cfg.loop_mode else 0.0
    return prog, geom


def decode(raw, geom, hal):
    """Raw capture -> per-channel arrays cropped to [trigger - pretrigger, + depth), times relative to the trigger."""
    from . import packet
    layout = _layout(geom)
    head = packet.parse_head(raw.head)
    trig = packet.trigger_index(head, layout) if head.get('triggered') else None
    return _decode_bytes(bytes(raw.data), geom, hal, layout, trig)


def decode_frames(raw, geom, hal):
    """Repeat mode: one dict per frame (a frame is its data + 4 KiB tail), each with its own trigger index.

    The bytes a frame carries beyond the record are device buffer content, not signal (see packet.split_frames);
    decode() keeps only what the frame's tail marks valid.
    """
    from . import packet
    layout = _layout(geom)
    out = []
    for frame in getattr(raw, 'frames', []) or []:
        dec = packet.decode(bytes(frame), layout, fill_stale=True)
        tail = dec.get('tail')
        trig = packet.trigger_index(tail, layout) if (tail is not None and layout.pretrig is not None) else None
        item = _decode_bytes(bytes(frame), geom, hal, layout, trig)
        item['trig_index'] = trig
        out.append(item)
    return out


def _layout(geom):
    from . import packet
    rates = {'analog': geom['analog_rate'], 'digital': geom['digital_rate']}
    return packet.layout_from_regs(geom['_regs'], rates=rates, trig_domain=geom['trig_domain'])


def _decode_bytes(data, geom, hal, layout, trig):
    from . import packet
    dec = packet.decode(data, layout, fill_stale=True)
    an_names = ['A%d' % i for i, a in enumerate(hal['analog']) if a['enable']]
    dg_names = ['D%d' % i for i, d in enumerate(hal['digital']) if d]
    out = {'analog': {}, 'digital': {}, 't0_analog': 0.0, 't0_digital': 0.0}
    dom = geom['trig_domain']
    for kind, names, depth in (('analog', an_names, geom['analog_depth']), ('digital', dg_names, geom['digital_depth'])):
        if not names:
            continue
        rate = geom[kind + '_rate']
        arrs = dec[kind]
        # trigger position in this domain's samples
        if trig is not None:
            dom_rate = geom[dom + '_rate']
            t_idx = trig * rate / dom_rate
            pre = geom['sample_index'] * rate / dom_rate
        else:
            t_idx = depth / 2.0
            pre = depth / 2.0
        start = int(round(t_idx - pre))
        start = max(0, start)
        for n in names:
            a = np.asarray(arrs[n] if n in arrs else arrs[int(n[1:])])
            out[kind][n] = np.ascontiguousarray(a[start:start + depth])
        out['t0_' + kind] = (start - t_idx) / rate
    return out
