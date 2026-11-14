"""openm454 command line. Every command prints ONE JSON document on stdout (watch: one JSON line per capture).

Exit codes: 0 ok, 2 bad argument, 3 device not found, 4 busy (DSWave / another openm454), 5 no trigger / timeout,
6 device or protocol error, 7 refused as unsafe, 1 other.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback

from .errors import DscopeError

from .paths import ROOT
DEFAULT_OUT = os.path.join(ROOT, 'captures')
STOP_REQUEST = os.path.join(ROOT, 'state', 'stop.request')
STREAM_MIN_RATE = 100e3          # default stream rate floor (samples/s per channel) when --samples/--rate are not given
_HEARTBEAT = [None]              # watch: called after every capture to re-arm the watchdog
_IDLE = [None]                   # called when the device is idle again (host post-processing follows)
_OUT = {'lock': threading.Lock(), 'owner': None}     # one output owner: main thread, or the watchdog once it fires


FOREVER = ('forever', 'inf', 'until-stop')


def is_forever(text):
    return bool(text) and text.strip().lower() in FOREVER


def stream_settings(args):
    """Settings for 'stream': untriggered, 10 s by default, at least STREAM_MIN_RATE unless the user chose."""
    from .scope import settings_from_args
    st = settings_from_args(args)
    if not (args.duration or args.timebase):
        st.duration = 10.0
    if not (args.samples or args.rate):
        st.samples = max(st.samples, int(st.duration * STREAM_MIN_RATE))
    st.trig_source, st.trig_mode = None, 'force'
    return st


def stop_requested():
    """True once 'openm454 stop' was run (watch/stream delete stale requests when they start)."""
    return os.path.exists(STOP_REQUEST)


def _clear_stop_request():
    try:
        os.remove(STOP_REQUEST)
    except OSError:
        pass


def _throttled(fn, period=0.1):
    last = [0.0, False]

    def f():
        now = time.time()
        if now - last[0] >= period:
            last[0], last[1] = now, fn()
        return last[1]
    return f


def _emit(obj, owner=None):
    """Print one JSON document/line. After the watchdog has taken over the output, the main thread prints nothing."""
    with _OUT['lock']:
        if _OUT['owner'] is not None and _OUT['owner'] != owner:
            return
        sys.stdout.write(json.dumps(obj, ensure_ascii=True, separators=(',', ':')) + '\n')
        sys.stdout.flush()


def _idle(n_values=0):
    if _IDLE[0]:
        _IDLE[0](n_values)


def _n_values(cap):
    return sum(len(v) for v in cap.analog.values()) + sum(len(v) for v in cap.digital.values())


def _fwd(p):
    return p.replace('\\', '/') if isinstance(p, str) else p


# ------------------------------------------------------------------------------------------- arguments
AWG_SPEC_HELP = ("also set the signal generator first: 'WAVE,FREQ,LOW,HIGH[,DUTY]', e.g. 'sine,10k,0V,2V' or "
                 "'square,1k,0V,3V,25%%' (0..3 V; it keeps running after the command)")
MEASURE_HELP = ('basic (default): the standard measurements per channel (analog: min/max/mean/pp/rms, top/base, '
                'frequency, duty, widths, rise/fall, overshoot, clipping; digital: state, frequency, duty, edges); '
                'full: basic plus cycle-by-cycle statistics (cycles, period/frequency min/max/std, pulse width and '
                'duty min/max, digital mean widths) and each analog channel\'s visible range range_lo_v/range_hi_v '
                '(slower on deep records); none: no measurements')


def _capture_args(p, cmd='capture'):
    _vertical_args(p)
    _timing_trigger_output_args(p, cmd)


def _vertical_args(p):
    g = p.add_argument_group('channels / vertical')
    g.add_argument('-c', '--channels', default='A0-A3,D0-D7',
                   help="channels to record (default A0-A3,D0-D7): e.g. 'A1', 'A0,A2,D0-D3', 'analog', 'digital', "
                        "'all'; fewer channels allow higher sample rates")
    g.add_argument('--vdiv', default='1V', help="volts/div at the probe tip, the screen is 8 divisions: '1V' or per "
                                                "channel 'A0=500mV,A2=2V' (default 1V; 1mV..5V x probe in 1-2-5 steps, "
                                                "rounded up)")
    g.add_argument('--offset', default='0V', help="voltage at the vertical centre: '0V' or per channel 'A0=1.5V' "
                                                  "(default 0V); at most about +/-4.3 x V/div, a larger one is clamped "
                                                  "with a warning; write --offset=-1.5V for a negative value")
    g.add_argument('--coupling', default='dc', help="'dc' (default) or 'ac', or per channel 'A1=ac'")
    g.add_argument('--bw20', default='', help='analog channels with the 20 MHz bandwidth limit, e.g. A1,A2 (default none)')
    g.add_argument('--invert', default='', help='analog channels to invert, e.g. A2 (default none)')
    g.add_argument('--probe', default=None, help="probe attenuation: '10x' or per channel 'A2=10x' (default 1x); "
                                                 "--vdiv, --offset and trigger levels are then at the probe tip")
    g.add_argument('--threshold', default='1.4V', help='logic threshold of the digital inputs, 0..5 V (default 1.4V)')


def _timing_trigger_output_args(p, cmd='capture'):
    """Options of capture, watch, stream and debug plan (cmd selects the help texts that differ)."""
    stream = cmd == 'stream'
    g = p.add_argument_group('timing')
    g.add_argument('--timebase', default=None, help='time/div, the window is 10 divisions: e.g. 500us = a 5 ms window')
    if stream:
        g.add_argument('--duration', default=None, help="recording length, e.g. 60s, 2h or 'forever' (until openm454 "
                                                        "stop); default 10s, or 10 x --timebase")
        g.add_argument('--samples', default=None, help='samples per channel over the whole recording, e.g. 50M '
                                                       '(default: 100 kSa/s x the duration, at least 100k)')
    else:
        g.add_argument('--duration', default=None, help='window length, e.g. 10ms (default 10 x --timebase, else 5ms); '
                                                        'wins over --timebase')
        g.add_argument('--samples', default=None, help='samples per channel, e.g. 1M (default 100k, at least 1024; up '
                                                       'to 400M on one channel, fewer with more channels: 4 Gbit '
                                                       'sample memory)')
    g.add_argument('--rate', default=None, help='sample rate per channel, e.g. 10M (default: --samples over the '
                                                'window; overrides --samples); snapped down to a rate the scope has, at '
                                                'most 400M on one analog channel, 200M on two, 100M on three or four '
                                                '(digital only: 400M on up to 2, 200M on up to 4, 100M on 8); the '
                                                'JSON acq block shows the rate used')
    g = p.add_argument_group('trigger', 'stream records untriggered: these options are accepted and ignored'
                             if stream else None)
    g.add_argument('--trig', default=None,
                   help="trigger SRC:EDGE[:LEVEL] with EDGE rise, fall or any, e.g. A1:rise:1.4V, D0:fall, "
                        "A2:any:0.15V (an analog level defaults to the channel's --offset); pulse width: "
                        "A1:pulse>:1.4V:200us, D4:npulse<:50us (pulse>, pulse<, npulse>, npulse<); default: none, "
                        "the capture starts at once")
    g.add_argument('--trig-mode', default='auto', choices=['auto', 'normal', 'force'],
                   help='auto (default): wait --auto-after for the trigger, then capture untriggered (acq.triggered '
                        'false); normal: wait up to --timeout, else exit code 5; force: capture at once. Without '
                        '--trig every mode captures at once')
    g.add_argument('--trig-pos', default='50%', help='share of the window before the trigger, 0..100%% (default 50%%: '
                                                     'the trigger in the middle; 10%% shows mostly what follows it)')
    g.add_argument('--auto-after', default='0.3s', help='auto mode: how long to wait for the trigger before capturing '
                                                        'untriggered (default 0.3s)')
    g.add_argument('--timeout', default='5s', help='normal mode: how long to wait for the trigger, e.g. 30s (default 5s)')
    g.add_argument('--holdoff', default='0s', help='trigger holdoff, e.g. 1ms (default 0s); the first trigger can '
                                                   'come up to this much later')
    g.add_argument('--sensitivity', type=int, default=5, help='trigger noise margin 0..15 (default 5; DSWave "Margin")')
    g.add_argument('--trig-retries', type=int, default=None, metavar='N',
                   help='digital rise/fall trigger: the digital inputs have no hysteresis, so a slow edge chatters and '
                        'can fire the opposite-edge trigger; each capture is checked and re-armed up to N times when it '
                        'fired on the wrong edge (default 4; 0 = only check). acq.trigger_check reports the result')
    g = p.add_argument_group('output', {
        'watch': 'watch prints one JSON line per capture and saves nothing but --save-last: --dump, --no-plot, '
                 '--no-save and --tag have no effect',
        'stream': 'the final JSON document measures, plots and saves the last record',
        'plan': 'debug plan only compiles the settings: these options have no effect'}.get(cmd))
    g.add_argument('--measure', default='basic', choices=['basic', 'full', 'none'], help=MEASURE_HELP)
    g.add_argument('--edges', type=int, default=0, metavar='N',
                   help='also list the first N rising and falling edge times per channel (edges_rise_s / edges_fall_s, '
                        't=0 at the trigger; default 0)')
    g.add_argument('--dump', type=int, default=0, metavar='N',
                   help='add a decimated min/max/mean series of at most N points per channel (default 0: none)')
    g.add_argument('--window', default=None, help='measure, decode and plot only t0:t1 in seconds, t=0 at the trigger '
                                                  '(or the window centre), e.g. 0:2ms, :500us; write --window=-1ms:1ms '
                                                  'when t0 is negative')
    _decode_args(g)
    g.add_argument('--no-plot', action='store_true', help='do not write the .png plot')
    if stream:
        g.add_argument('--no-save', action='store_true', help='save no files (no .npz, .png or decode file)')
        g.add_argument('--out', default=DEFAULT_OUT, help='folder for the files (default: captures/ in the data folder)')
        g.add_argument('--tag', default='cap', help='file name tag, files are <date>-<time>_<tag>.npz/.png (default '
                                                    'stream)')
    else:
        g.add_argument('--no-save', action='store_true', help='do not save the .npz samples or the decode file (the '
                                                              '.png is still written unless --no-plot)')
        g.add_argument('--out', default=DEFAULT_OUT,
                       help='folder for the files (default: captures/ in the data folder); capture keeps the newest '
                            '200 captures with its --tag there and deletes older ones (only files it named itself)')
        g.add_argument('--tag', default='cap', help='file name tag, files are <date>-<time>_<tag>.npz/.png (default '
                                                    'cap)')
    g.add_argument('--awg', default=None, help=AWG_SPEC_HELP)


def _decode_args(g):
    g.add_argument('--decode', action='append', default=[], metavar='SPEC',
                   help="protocol decoder, repeatable: 'uart:rx=D4,baud=115200[,invert=1]', "
                        "'spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=0', 'i2c:scl=D6,sda=D7', 'can:rx=D6,bitrate=500k'")
    g.add_argument('--decode-limit', type=int, default=50,
                   help='frames listed per decoder in the JSON (default 50); capture and stream also save the complete '
                        'list to <file>_decode.json')


def build_parser():
    ap = argparse.ArgumentParser(prog='openm454', description='DSCope M454 oscilloscope / logic analyzer / AWG for agents')
    ap.add_argument('--max-wall', default=None,
                    help='watchdog: stop and release the device after this time, e.g. 10min (default: computed from '
                         'the command; a value below the computed one is ignored)')
    ap.add_argument('--verbose', action='store_true', help='USB wire log (register reads and writes) on stderr')
    ap.add_argument('--json', action='store_true', help='JSON output (always on: every command prints JSON; the flag is '
                                                         'accepted so scripts can say so)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('info', help='device, firmware, calibration, limits')
    c = sub.add_parser('capture', help='single triggered capture -> JSON measurements + PNG + NPZ')
    _capture_args(c)
    c.add_argument('--repeat', type=int, default=0, metavar='N',
                   help='hardware repeat mode: collect N back-to-back frames in one start (default 0 = one capture); '
                        'forces --trig-mode force, each frame has its own trigger point')
    w = sub.add_parser('watch', help='repeated captures, one JSON line each (real-time monitoring)')
    _capture_args(w, 'watch')
    g = w.add_argument_group('watch')
    g.add_argument('--every', default='0.5s', help='from the start of one capture to the start of the next, e.g. 0.5s, '
                                                   '2s, 1min (default 0.5s); a capture that takes longer is followed '
                                                   'at once. Each line\'s t_s is its capture\'s start')
    g.add_argument('--count', type=int, default=None, metavar='N',
                   help='number of captures (default 20 when neither --count nor --for is given)')
    g.add_argument('--for', dest='duration_total', default=None, help='total watch time, e.g. 30s, 10min, 2h: no '
                                                                      'capture starts after it (with --count: '
                                                                      'whichever ends first)')
    g.add_argument('--fields', default=None,
                   help='comma list of CHANNEL.KEY printed per line, KEY any measurement of the capture JSON, e.g. '
                        'A1.freq_hz,A1.vpp_v,D0.state (default: vpp_v, vmean_v, freq_hz, duty_pct and state of every '
                        'channel); only these channels are measured')
    g.add_argument('--stop-when', default=None,
                   help="stop when this Python expression over the measurements is true, e.g. \"A1['vpp_v'] < 1\" "
                        "(one dict per channel; an expression that fails counts as false)")
    g.add_argument('--save-last', action='store_true', help='save the last capture as <out>/<date>-<time>_watch_last.npz')
    sm = sub.add_parser('stream', help='untriggered recording streamed while it is acquired, with live JSON statistics; '
                                       'longer than one record (400M samples / 512 MiB) it refreshes record after record')
    _capture_args(sm, 'stream')
    g = sm.add_argument_group('stream')
    g.add_argument('--every', default='1s', help='signal time per live statistics line (default 1s)')
    g.add_argument('--save-each', action='store_true', help='save every record (default: only the last one)')
    g.add_argument('--record-first', action='store_true',
                   help="above ~100 MB/s acquire each record first and read it out afterwards (DSWave's way); by "
                        "default the record is sent while it is acquired, so the next one starts sooner")
    lg = sub.add_parser('log', help='gap-free endless recording (loop mode) at one rate for all channels, kept in RAM '
                                    '(default) or dumped to disk; --plan shows rate limits, duration and storage')
    _vertical_args(lg)
    lg.add_argument('--rate', default='1M', help='sample rate of every channel: 100 MHz / N for any integer N '
                                                  '(1k..100M, e.g. 33.333M = 100M/3; default 1M); '
                                                  '200M with <= 2 analog and <= 4 digital channels, 400M with <= 1 analog '
                                                  'and <= 2 digital (interleaved)')
    lg.add_argument('--duration', default='10s', help="e.g. 60s, 2h, or 'forever' (until openm454 stop); default 10s")
    lg.add_argument('--every', default='1s', help='signal time per live statistics line (default 1s)')
    lg.add_argument('--storage', default='ram', choices=['ram', 'disk', 'none'],
                    help='ram (default): keep the samples in RAM, write one .npz at the end; disk: dump chunk files '
                         'while running (wears an SSD at high rates: only for records needed in full); none: '
                         'statistics only')
    lg.add_argument('--keep-last', action='store_true', help='RAM log longer than the budget: keep the newest part '
                                                             '(a ring) instead of refusing ("forever" always does)')
    lg.add_argument('--chunk', default=None, help='disk storage: seconds per file (default: ~256 MB per file)')
    lg.add_argument('--ram', default=None, help='RAM budget (default: the allowance set with openm454 bench --ram, else '
                                                 'min(4 GB, a quarter of the free RAM))')
    lg.add_argument('--plan', action='store_true', help='print the plan (rate verdict, max duration, storage) and exit '
                                                         'without touching the device')
    lg.add_argument('--no-save', action='store_true', help='same as --storage none')
    lg.add_argument('--force', action='store_true', help='above the readout of this channel set, log anyway for longer '
                                                         'than the gap-free limit: records that much, then stops '
                                                         '(ring_full); without it such a log is refused (NEEDS_FORCE, '
                                                         'or STREAM_BETTER when stream does better)')
    lg.add_argument('--remeasure', action='store_true', help='measure the readout of this channel set again before the run '
                                                         '(otherwise measured once, when unknown, and remembered)')
    lg.add_argument('--out', default=None, help='output folder (default: the data folder set with openm454 bench '
                                                 '--dir, else captures/)')
    lg.add_argument('--tag', default='log', help='file name tag, files are <date>-<time>_<tag>... (default log)')
    lg.add_argument('--awg', default=None, help=AWG_SPEC_HELP)
    lg.add_argument('--hook', default=None, metavar='FILE.py',
                    help='live processing: a Python file with process(block) (optional setup(info), finish(summary)), '
                         'called with every piece of the recording (~10 ms of signal) as it arrives; a returned dict is '
                         'printed at once as an {"event": ...} line, {"stop": true} in it ends the recording '
                         '(stop_reason hook). See openm454/hook.py')
    bn = sub.add_parser('bench', help="this PC's limits for recording: device status, free RAM, decode speed, disk "
                                      "write speed, USB readout; records the owner's RAM / disk allowance")
    bn.add_argument('--ram', default=None, help='RAM the owner allows for logs, e.g. 8GB (stored in state/host.json)')
    bn.add_argument('--disk', default=None, help='disk space the owner allows for disk logs, e.g. 50GB')
    bn.add_argument('--dir', default=None, help='data folder for logs (stored; default --out of log)')
    bn.add_argument('--disk-test-mb', type=int, default=256, help='size of the disk write test in MB (default 256; '
                                                                  'written once, deleted)')
    bn.add_argument('--no-device', action='store_true', help='host measurements only (no USB readout test)')
    a = sub.add_parser('awg', help='signal generator on the MCX output (0..3 V); it keeps running after the command')
    asub = a.add_subparsers(dest='awg_cmd', required=True)
    s = asub.add_parser('set', help='continuous sine, square, triangle or noise')
    s.add_argument('--wave', default='sine', choices=['sine', 'square', 'triangle', 'noise'], help='waveform (default sine)')
    s.add_argument('--freq', default='1k', help='frequency, e.g. 10k (default 1k; 0.1 Hz .. 1 MHz, triangle up to 400 kHz)')
    s.add_argument('--high', default=None, help='high level, e.g. 2V (default 3V); 0 <= low < high <= 3 V')
    s.add_argument('--low', default=None, help='low level (default 0V)')
    s.add_argument('--vpp', default=None, help='peak-to-peak amplitude instead of --high/--low, centred on --dc')
    s.add_argument('--dc', default=None, help='centre voltage with --vpp (default vpp/2: the low level at 0 V)')
    s.add_argument('--duty', default='50%', help='square: duty cycle, triangle: symmetry; 0.1..99.9%% (default 50%%; '
                                                 'square pulses at least 380 ns)')
    s.add_argument('--phase', default='0', help='start phase in whole degrees (default 0)')
    sw = asub.add_parser('sweep', help='frequency sweep start -> end in --time (repeats)')
    sw.add_argument('--start', required=True, help='start frequency, e.g. 1k')
    sw.add_argument('--end', required=True, help='end frequency, e.g. 100k (below --start: sweeps down)')
    sw.add_argument('--time', default='100ms', help='sweep time 1 ms .. 500 s (default 100ms)')
    sw.add_argument('--log', action='store_true', help='logarithmic sweep (default linear)')
    bu = asub.add_parser('burst', help='--cycles periods of the carrier every --period')
    bu.add_argument('--cycles', type=int, default=1, help='carrier periods per burst (default 1; 1..100, fewer for '
                                                          'carriers below 7 Hz)')
    bu.add_argument('--period', default='10ms', help='from one burst to the next (default 10ms; (cycles + 0.5) / '
                                                     'freq .. 15 s)')
    mo = asub.add_parser('mod', help='AM / FM / FSK of the carrier (internal source)')
    mo.add_argument('--kind', choices=['am', 'fm', 'fsk'], default='am', help='modulation (default am)')
    mo.add_argument('--mod-wave', choices=['sine', 'triangle', 'square'], default='sine',
                    help='modulating waveform (default sine)')
    mo.add_argument('--mod-freq', default='100', help='modulation frequency, FSK: the keying rate (default 100 Hz; '
                                                      '0.1 Hz .. 1 MHz, FM: at most the carrier)')
    mo.add_argument('--depth', default='50%', help='AM depth (default 50%%; the maximum depends on --high/--low)')
    mo.add_argument('--deviation', default=None, help='FM peak deviation in Hz (needed for fm; at most the carrier '
                                                      'frequency)')
    mo.add_argument('--hop', default=None, help='FSK: the second frequency in Hz, absolute (needed for fsk)')
    for x in (sw, bu, mo):
        x.add_argument('--wave', default='sine', choices=['sine', 'triangle', 'square'],
                       help='carrier waveform (default sine)')
        x.add_argument('--high', default='3V', help='high level (default 3V); 0 <= low < high <= 3 V')
        x.add_argument('--low', default='0V', help='low level (default 0V)')
        x.add_argument('--duty', default='50%', help='square: duty cycle, triangle: symmetry 10..90%% (default 50%%)')
    for x in (bu, mo):
        x.add_argument('--freq', default='1k', help='carrier frequency (default 1k)')
    dcp = asub.add_parser('dc', help='steady DC level (0..3 V, 6.4 mV steps)')
    dcp.add_argument('--level', required=True, help='output level, e.g. 1.2V')
    asub.add_parser('off', help='stop the output and park it at 0 V')
    asub.add_parser('status', help='the last setting made by openm454 (the generator cannot be read back)')
    pl = sub.add_parser('plot', help='render a saved capture (.npz) to PNG (no device)')
    pl.add_argument('file', help='a saved .npz file')
    pl.add_argument('--window', default=None, help='zoom to t0:t1 in seconds, e.g. 0:2ms or --window=-1ms:1ms; the png '
                                                   'is then <file>_zoom.png')
    pl.add_argument('--channels', default=None, help='channels to draw, e.g. A1,D0 (default: all in the file)')
    pl.add_argument('--out', default=None, help='png path (default: <file>.png next to the file)')
    an = sub.add_parser('analyze', help='measure a saved capture (.npz) (no device)')
    an.add_argument('file', help='a saved .npz file')
    an.add_argument('--window', default=None, help='measure and decode only t0:t1 in seconds, e.g. 0:2ms or '
                                                   '--window=-1ms:1ms')
    an.add_argument('--edges', type=int, default=0, metavar='N',
                    help='also list the first N rising and falling edge times per channel (default 0)')
    an.add_argument('--dump', type=int, default=0, metavar='N',
                    help='add a decimated min/max/mean series of at most N points per channel (default 0: none)')
    an.add_argument('--channels', default=None, help='channels to report, e.g. A1,D0 (default: all in the file)')
    an.add_argument('--measure', default='basic', choices=['basic', 'full', 'none'], help=MEASURE_HELP)
    _decode_args(an)
    st = sub.add_parser('selftest', help='verify the scope: wiring-free checks on any wiring, signal checks on the '
                                         'standard bench fixture')
    st.add_argument('--quick', action='store_true', help='fewer repetitions (about half the time)')
    st.add_argument('--no-awg', action='store_true', help='never touch the signal generator (skips V10)')
    st.add_argument('--fixture', choices=['file', 'standard'], default='file',
                    help='file (default): the wiring recorded in state/fixture.json; standard: the standard bench '
                         'fixture is wired right now (this run only)')
    su = sub.add_parser('setup', help='where the FPGA bitstream comes from (DSWave 1.2.1): show it, or record the '
                                      'DSWave folder once for every openm454 process, the MCP server included (no USB)')
    su.add_argument('--dswave', default=None, metavar='FOLDER',
                    help='DSWave 1.2.1 folder (or its DSWave.exe): checked, the bitstream cached, the path saved in '
                         'state/dswave.json of the data folder')
    sub.add_parser('release', help='make sure the device is idle and released')
    sub.add_parser('recover', help='reset and reload the FPGA (like a power-up) when the scope hangs; a scope '
                                   'that does not answer register reads still needs a replug')
    sub.add_parser('stop', help='ask a running watch/stream/log (e.g. started in the background) to stop cleanly; '
                                'never kill those processes')
    d = sub.add_parser('debug', help='maintenance only (not for experiments)')
    dsub = d.add_subparsers(dest='debug_cmd', required=True)
    r = dsub.add_parser('regs-read', help='read device registers')
    r.add_argument('addr', help='register address, e.g. 0x10')
    r.add_argument('len', nargs='?', default='2', help='bytes to read, 1..127 (default 2)')
    r.add_argument('ext', nargs='?', default='0', help='extension byte of the read request (default 0)')
    r = dsub.add_parser('regs-write', help='write one register (refused for MODE/CTRL/ACTION/SYNC and most MCU '
                                           'registers 0xF0-0xFF)')
    r.add_argument('addr', help='register address, e.g. 0x10')
    r.add_argument('value', help='value, e.g. 0x0001')
    r = dsub.add_parser('plan', help='print the register program for capture options without touching the device')
    _capture_args(r, 'plan')
    return ap


# ------------------------------------------------------------------------------------------- commands
def cmd_capture(args, scope, emit=True):
    from .scope import settings_from_args
    from . import plot as plotmod
    from .units import window
    st = settings_from_args(args)
    if args.awg:
        scope.awg_from_spec(args.awg)
    cap = scope.capture(st)
    _idle(_n_values(cap))
    win = window(args.window)
    res = {'ok': True, 'cmd': 'capture', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
    _report(cap, args, res, win)
    decoded = _decode(cap, args, win, res)
    files = {}
    if not args.no_save:
        from .capture import new_path, prune
        stem = new_path(args.out, args.tag)
        files['npz'] = _fwd(cap.save(stem + '.npz'))
        if decoded:
            files['decode'] = _fwd(_save_json(stem + '_decode.json', decoded))
        if not args.no_plot:
            files['png'] = _fwd(plotmod.plot(cap, stem + '.png', window=win, title=_title(cap)))
        prune(args.out, tag=args.tag)
    elif not args.no_plot:
        from .capture import new_path
        stem = new_path(args.out, args.tag)
        files['png'] = _fwd(plotmod.plot(cap, stem + '.png', window=win, title=_title(cap)))
    res['files'] = files
    res['hints'] = cap.meta.get('hints', [])
    res['warnings'] = cap.meta.get('warnings', [])
    if emit:
        _emit(res)
    return cap, res


FULL_EDGES = 100_000     # --measure full: cycle statistics over at most this many edges of each direction


def _measurements(cap, args, win=None, channels=None):
    """{'analog': {ch: ..}, 'digital': {ch: ..}} as --measure asks: none -> {}; basic -> Capture.summary; full ->
    basic plus cycle statistics (_cycle_stats) and each analog channel's visible range. --edges N lists edge times."""
    mode = getattr(args, 'measure', 'basic')
    if mode == 'none':
        return {}
    edges = getattr(args, 'edges', 0) or 0
    n = max(edges, FULL_EDGES) if mode == 'full' else edges
    kw = {'channels': channels} if channels is not None else {}
    if win:
        kw['window'] = win
    if n:
        kw['edges_n'] = n
    s = cap.summary(**kw)
    if mode == 'full':
        for grp in ('analog', 'digital'):
            for ch, m in s[grp].items():
                for k, v in _cycle_stats(m, n).items():
                    m.setdefault(k, v)                 # digital gains pwidth_s / nwidth_s, analog has them already
                if grp == 'analog':
                    m.update(_visible_range(cap, ch))
                for k in ('edges_rise_s', 'edges_fall_s'):
                    if edges and k in m:
                        m[k] = m[k][:edges]
                    else:
                        m.pop(k, None)
    return s


def _cycle_stats(m, limit):
    """Statistics over every whole cycle (rising edge to rising edge) of a measurement made with edge lists: period
    and frequency min/max/std, high (pwidth) and low (nwidth) time min/max/mean and duty min/max. Nothing for fewer
    than 2 rising edges or noise (measure.analog noise_like)."""
    import numpy as np
    from .measure import _r
    r = np.asarray(m.get('edges_rise_s') or [], dtype=float)
    f = np.asarray(m.get('edges_fall_s') or [], dtype=float)
    if r.size < 2 or m.get('noise_like'):
        return {}
    per = np.diff(r)
    out = {'cycles': int(per.size), 'period_min_s': _r(per.min(), 7), 'period_max_s': _r(per.max(), 7),
           'period_std_s': _r(per.std()), 'freq_min_hz': _r(1.0 / per.max(), 7), 'freq_max_hz': _r(1.0 / per.min(), 7)}
    if r.size >= limit or f.size >= limit:
        out['cycles_capped'] = True                    # the statistics cover the first `limit` edges only
    k = np.searchsorted(f, r[:-1], side='right')       # first fall after each rise ...
    ok = k < f.size
    ok[ok] = f[k[ok]] < r[1:][ok]                      # ... within the same cycle
    if ok.any():
        hi = f[k[ok]] - r[:-1][ok]
        lo = per[ok] - hi
        duty = hi / per[ok] * 100
        out.update(pwidth_s=_r(hi.mean()), nwidth_s=_r(lo.mean()), pwidth_min_s=_r(hi.min()),
                   pwidth_max_s=_r(hi.max()), nwidth_min_s=_r(lo.min()), nwidth_max_s=_r(lo.max()),
                   duty_min_pct=_r(duty.min(), 4), duty_max_pct=_r(duty.max(), 4))
    return out


def _visible_range(cap, ch):
    """The voltages an analog channel can show at its V/div and offset (readings at the ends are clipped)."""
    from .capture import CODE_MIN, CODE_MAX
    from .measure import _r
    s = cap.scale(ch)
    k = s['volts_per_code'] * (s.get('probe') or 1.0)
    return {'range_lo_v': _r((CODE_MIN - s['zero_code']) * k, 4), 'range_hi_v': _r((CODE_MAX - s['zero_code']) * k, 4)}


def _report(cap, args, res, win):
    """Measurements (--measure, --edges) and the --dump series of a capture into res."""
    res.update(_measurements(cap, args, win))
    if args.dump:
        res['dump'] = {ch: cap.dump(ch, args.dump, win) for ch in cap.channels()}


def _decode(cap, args, win, res):
    """Run every --decode spec; res gets the size-limited results, the full ones are returned."""
    from .decode import decode, trim
    full = [decode(cap, spec, win, None) for spec in getattr(args, 'decode', None) or ()]
    if full:
        res['decode'] = [trim(r, args.decode_limit) for r in full]
    return full


def _save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, separators=(',', ':'))
    return path


def _title(cap):
    a = cap.meta['applied']; q = cap.meta['acq']
    tr = a.get('trigger', {})
    return 'DSCope M454  %s  %s Sa/s  %d samples  trig %s %s %s  %s' % (
        ','.join(a.get('channels', [])), '%.4g' % q['analog_rate_hz'] if q.get('analog_rate_hz') else '%.4g' % q.get('digital_rate_hz', 0),
        q.get('samples', 0), tr.get('source', '-'), tr.get('edge', ''), tr.get('level_v', ''),
        'triggered' if q.get('triggered') else 'NOT triggered')


def _watch_fields(args):
    """-> (fields or None, channels to measure or None = all). --fields / --stop-when need measurements."""
    fields = args.fields.split(',') if args.fields else None
    if args.measure == 'none' and (fields or args.stop_when):
        raise DscopeError('INVALID_ARG', '--fields and --stop-when read measurements: they do not work with --measure '
                                         'none')
    need = None                              # with --fields, measure only the channels the output names
    if fields:
        import re
        need = {f.split('.')[0] for f in fields}
        if args.stop_when:
            need |= set(re.findall(r'\b[AD][0-7]\b', args.stop_when))
    return fields, need


def _wait_until(t, poll=0.25):
    """Sleep until time t (in slices, so a stop request is seen during a long --every); True when one came."""
    while True:
        left = t - time.time()
        if left <= 0:
            return False
        if stop_requested():
            return True
        time.sleep(min(left, poll))


def cmd_watch(args, scope):
    """Captures start --every apart, start to start, and each line's t_s is its capture's start (not its end: the
    first capture of a session is slower, it writes the setup tables and waits for the analog front end). After a
    capture longer than --every the next starts at once and the schedule continues from there (no catch-up burst)."""
    from .scope import settings_from_args
    from .units import si, window
    st = settings_from_args(args)
    every = si(args.every, 's', '--every')
    total = si(args.duration_total, 's', '--for') if args.duration_total else None
    win = window(args.window)
    fields, need = _watch_fields(args)
    if args.awg:
        scope.awg_from_spec(args.awg)
    t0 = due = time.time(); i = 0; hist = {}; stopped_by = 'count'
    last = None
    while True:
        start = time.time()
        cap = scope.capture(st)
        s = _measurements(cap, args, win, need)
        chans = {ch: m for grp in ('analog', 'digital') for ch, m in s.get(grp, {}).items()}
        flat = {'%s.%s' % (ch, k): v for ch, m in chans.items() for k, v in m.items()}
        keys = fields or [k for k in flat if k.split('.')[1] in ('vpp_v', 'freq_hz', 'duty_pct', 'state', 'vmean_v')]
        line = {'i': i, 't_s': round(start - t0, 3), 'triggered': cap.meta['acq'].get('triggered')}
        if cap.meta['acq'].get('trigger_check') not in (None, 'unchecked', 'ok'):
            line['trigger_check'] = cap.meta['acq']['trigger_check']
        if cap.meta['acq'].get('trigger_misfires'):
            line['trigger_misfires'] = cap.meta['acq']['trigger_misfires']
        if getattr(args, 'decode', None):
            line['decode'] = [{k: r.get(k) for k in ('protocol', 'count', 'errors', 'frames') if k in r}
                              for r in _decode(cap, args, win, {})]
            for r in line['decode']:
                r['frames'] = r['frames'][:args.decode_limit]
        for k in keys:
            line[k] = flat.get(k)
            if isinstance(flat.get(k), (int, float)) and not isinstance(flat.get(k), bool):
                hist.setdefault(k, []).append(flat[k])
        _emit(line)
        if _HEARTBEAT[0]:
            _HEARTBEAT[0]()
        last = cap; i += 1
        if args.stop_when:
            try:
                if eval(args.stop_when, {'__builtins__': {}}, chans):
                    stopped_by = 'stop_when'; break
            except Exception:
                pass
        if stop_requested():
            stopped_by = 'stop_request'; _clear_stop_request(); break
        if args.count is not None and i >= args.count:
            stopped_by = 'count'; break
        if args.count is None and total is None and i >= 20:
            stopped_by = 'default_count_20'; break
        due = max(due + every, time.time())    # start to start; an overrun restarts the schedule now
        if total is not None and due - t0 >= total:
            stopped_by = 'duration'; break
        if _wait_until(due):
            stopped_by = 'stop_request'; _clear_stop_request(); break
    import statistics as stx
    summ = {k: {'min': min(v), 'max': max(v), 'mean': stx.fmean(v), 'std': stx.pstdev(v) if len(v) > 1 else 0.0}
            for k, v in hist.items()}
    out = {'summary': True, 'n': i, 'stopped_by': stopped_by, 'elapsed_s': round(time.time() - t0, 2), 'fields': summ}
    if args.save_last and last is not None:
        from .capture import new_path
        stem = new_path(args.out, 'watch_last')
        out['files'] = {'npz': _fwd(last.save(stem + '.npz'))}
    _emit(out)


def stream_plan(args):
    """-> (settings of one record, total seconds or None for 'forever', seconds per record, chained).
    One record holds at most 400M samples per channel and 512 MiB of sample memory; a longer stream (or
    'forever') is cut into records of the same rate, each refreshing the previous one (~0.2 s between them)."""
    import copy
    from .scope import stream_record_plan
    a = copy.copy(args)
    forever = is_forever(args.duration)
    if forever:
        a.duration = None
    st = stream_settings(a)
    total = None if forever else st.duration
    rec, rec_s, chained = stream_record_plan(st, total, STREAM_MIN_RATE)
    return rec, total, rec_s, chained


def cmd_stream(args, scope):
    from .units import si, window
    st, total, rec_s, chained = stream_plan(args)
    win = window(args.window)                      # the final document: measurements, dump, decode and plot
    if args.awg:
        scope.awg_from_spec(args.awg)
    every = si(args.every, 's', '--every')
    if not chained:
        try:
            cap = scope.stream(st, every=every, emit=_emit, stop_flag=_throttled(stop_requested),
                               live=not args.record_first)
        except DscopeError as e:
            if e.code != 'STOPPED':
                raise
            _clear_stop_request()
            _emit({'ok': True, 'cmd': 'stream', 'stopped_by': 'stop_request', 'samples': 0, 'note': e.message})
            return
        _idle(_n_values(cap))
        if stop_requested():
            _clear_stop_request()
        res = {'ok': True, 'cmd': 'stream', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
        _report(cap, args, res, win)
        _stream_files(args, cap, res, win)
        _emit(res)
        return
    import copy
    k, done, seams, prev_end, last, saved, stopped_by = 0, 0.0, [], None, None, [], None
    prev = None                                    # the previous record, not decoded (kept raw until replaced)
    while total is None or done < total - 1e-9:
        st_k = copy.copy(st)
        st_k.duration = rec_s if total is None else min(rec_s, total - done)
        is_last = total is not None and total - done <= rec_s * (1 + 1e-9)

        def emit_k(line, k=k, off=done):
            line = dict(line, record=k, t_signal_s=round(off + line['t_signal_s'], 4))
            if line.pop('final', False):
                line['record_end'] = True
            _emit(line)
        try:
            cap = scope.stream(st_k, every=every, emit=emit_k, stop_flag=_throttled(stop_requested),
                               build=lambda last_one=is_last: last_one or args.save_each or stop_requested(),
                               live=not args.record_first)
        except DscopeError as e:
            if e.code != 'STOPPED':
                raise
            stopped_by = 'stop_request'
            if prev is not None:                   # stopped before this record had data: keep the previous one
                last = scope.build_capture(prev)
            break
        acq = cap.meta['acq']
        if prev_end is not None and acq.get('t_start_wall'):
            seams.append(round(acq['t_start_wall'] - prev_end, 3))
        prev_end = acq['t_start_wall'] + acq['duration_s'] if acq.get('t_start_wall') else None
        done += acq['duration_s']
        prev = cap
        if cap.meta.get('kept', True):
            last = cap
            if args.save_each and not is_last:
                res_k = {}
                _stream_files(args, cap, res_k, win, tag='stream%03d' % k, plot=False)
                saved.append(res_k['files'])
        k += 1
        if _HEARTBEAT[0]:
            _HEARTBEAT[0]()
        if stop_requested() or not acq.get('complete', True):
            stopped_by = 'stop_request' if stop_requested() else 'incomplete record'
            last = scope.build_capture(cap)
            break
    if stop_requested():
        _clear_stop_request()
    res = {'ok': True, 'cmd': 'stream', 'records': k, 'record_s': round(rec_s, 6), 'signal_s': round(done, 6),
           'seams_s': seams[:100], 'seam_max_s': max(seams) if seams else None, 'stopped_by': stopped_by,
           'note': 'each record refreshes the previous one: only the last record is kept (--save-each keeps all; '
                   '"log" records without gaps)'}
    if last is not None:
        _idle(_n_values(last))
        res.update({'applied': last.meta['applied'], 'acq': last.meta['acq']})
        _report(last, args, res, win)
        _stream_files(args, last, res, win)
        if saved:
            res['files']['records'] = saved
    _emit(res)


def _stream_files(args, cap, res, win=None, tag=None, plot=True):
    from . import plot as plotmod
    decoded = _decode(cap, args, win, res)
    files = {}
    if not args.no_save:
        from .capture import new_path
        stem = new_path(args.out, tag or (args.tag if args.tag != 'cap' else 'stream'))
        files['npz'] = _fwd(cap.save(stem + '.npz'))
        if decoded:
            files['decode'] = _fwd(_save_json(stem + '_decode.json', decoded))
        if plot and not args.no_plot:
            files['png'] = _fwd(plotmod.plot(cap, stem + '.png', window=win, title=_title(cap)))
    res['files'] = files
    res['hints'] = cap.meta.get('hints', [])
    res['warnings'] = cap.meta.get('warnings', [])


def _log_storage(args):
    return 'none' if args.no_save else args.storage


def _log_out(args):
    from . import host
    return args.out or host.data_dir(DEFAULT_OUT)


def _log_hook(args):
    """The --hook file loaded once (before the scope is opened, so a broken file is refused at once), or None."""
    if not getattr(args, 'hook', None):
        return None
    if getattr(args, '_hook_obj', None) is None:
        from .hook import Hook
        args._hook_obj = Hook(args.hook)
    return args._hook_obj


def log_plan(args, zero_info=None, defer=False):
    """-> (settings, plan) of a 'log' command (offline). defer: an unmeasured readout (or --remeasure) refuses
    nothing yet; the run measures it first."""
    from .scope import vertical_from_args
    from .units import si
    from . import logger
    st = vertical_from_args(args)
    duration = None if is_forever(args.duration) else si(args.duration, 's', '--duration')
    storage = _log_storage(args)
    pl = logger.plan(st, si(args.rate, 'Hz', '--rate'), duration, si(args.every, 's', '--every'),
                     None if storage == 'none' else _log_out(args), storage,
                     int(si(args.ram, 'B', '--ram')) if args.ram else None,
                     si(args.chunk, 's', '--chunk') if args.chunk else None, zero_info=zero_info, force=args.force,
                     keep_last=args.keep_last, defer=defer, remeasure=defer and args.remeasure)
    return st, pl


def cmd_log(args, scope):
    from . import logger
    from .capture import new_path
    st, pl = log_plan(args, scope.dev.zero_info, defer=True)
    scope._check_link()
    if pl['_needs_probe']:
        r, kept_up = logger.measure_readout(scope, pl)  # this channel set's readout, remembered in state/host.json
        _emit({'ok': True, 'cmd': 'log', 'probe': {'readout_mb_s': round(r / 1e6, 1), 'kept_up': kept_up,
                                                   'data_mb_s': round(pl['_data_bps'] / 1e6, 1)}})
        st, pl = log_plan(args, scope.dev.zero_info)    # now with the measured readout: may refuse
    if args.awg:                                        # only once the log will run
        scope.awg_from_spec(args.awg)
    stem = None if pl['storage'] == 'none' else new_path(_log_out(args), args.tag)
    _emit({'ok': True, 'cmd': 'log', 'started': True, 'plan': logger.public(pl), 'out_stem': _fwd(stem)})
    try:
        res = logger.run(scope, st, pl, stem, emit=_emit, stop_flag=_throttled(stop_requested),
                         heartbeat=_HEARTBEAT[0], on_final=_idle, hook=_log_hook(args))
    except (DscopeError, KeyboardInterrupt) as e:    # the data kept so far was written: say where
        files = {k: _fwd(v) for k, v in (getattr(e, 'log_files', None) or {}).items()}
        extra = {'hook': e.hook_report} if getattr(e, 'hook_report', None) else {}
        if isinstance(e, KeyboardInterrupt):
            _emit({'ok': False, 'cmd': 'log', 'error': {'code': 'INTERRUPTED', 'message': 'interrupted (the scope was '
                                                        'stopped first)'}, 'files': files, **extra})
            return 1
        _emit({'ok': False, 'cmd': 'log', 'error': e.to_dict(), 'files': files, **extra})
        return e.exit_code
    if stop_requested():
        _clear_stop_request()
    for k in ('npz', 'dir', 'index'):
        if k in res.get('files', {}):
            res['files'][k] = _fwd(res['files'][k])
    _emit(res)
    if not res['ok']:
        from .errors import EXIT_CODES
        return EXIT_CODES.get(res['error']['code'], 1)
    return 0


BENCH_READOUT = (('2 analog + 8 digital', ['A0', 'A1'], 100e6), ('4 analog + 8 digital', ['A0', 'A1', 'A2', 'A3'], 50e6))


def cmd_bench(args, scope=None):
    """Record the owner's allowances, measure this PC (and the USB readout when the scope is open), recommend."""
    from . import host, logger
    from .scope import Settings
    from .units import si
    h = host.load()
    allow = dict(h.get('allow', {}))
    if args.ram:
        allow['ram_bytes'] = int(si(args.ram, 'B', '--ram'))
    if args.disk:
        allow['disk_bytes'] = int(si(args.disk, 'B', '--disk'))
    if args.dir:
        allow['data_dir'] = os.path.abspath(args.dir)
    d = allow.get('data_dir') or DEFAULT_OUT
    b = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'ram_free_bytes': logger.available_ram(),
         'decode_mb_s': round(host.bench_decode(), 1)}
    w, free = host.bench_disk(d, args.disk_test_mb)
    b.update(disk_dir=_fwd(os.path.abspath(d)), disk_write_mb_s=round(w, 1), disk_free_bytes=free)
    res = {'ok': True, 'cmd': 'bench'}
    if scope is not None:
        res['device'] = scope.info()['device']
        readout = {}
        for name, analog, rate in BENCH_READOUT:     # a 2 s burst above the sustained rate, nothing kept
            st = Settings()
            st.analog, st.digital = list(analog), ['D%d' % i for i in range(8)]
            r = scope.record(st, rate, 2.0, every=1.0, storage='none', force=True, probe=False)   # remembers R
            readout[name] = {'data_mb_s': r['plan']['data_mb_s'], 'readout_mb_s': r['acq']['host_mb_s'],
                             'backlog_max_mb': r['acq']['backlog_max_mb']}
        b['usb_readout'] = readout
        b['usb_readout_mb_s'] = min(v['readout_mb_s'] for v in readout.values() if v['readout_mb_s'])
    else:                                          # --no-device keeps the last USB measurement
        for k in ('usb_readout', 'usb_readout_mb_s'):
            if k in h.get('bench', {}):
                b[k] = h['bench'][k]
    h = host.load()                                # the readout runs above remembered their channel sets
    h['allow'], h['bench'] = allow, b
    host.save(h)
    ram = logger.default_ram_budget()
    live = min(b.get('usb_readout_mb_s') or logger.CEILING_BPS / 1e6, 0.8 * b['decode_mb_s'])
    res.update(allow={'ram_gb': round(allow['ram_bytes'] / 1e9, 2) if allow.get('ram_bytes') else None,
                      'disk_gb': round(allow['disk_bytes'] / 1e9, 1) if allow.get('disk_bytes') else None,
                      'data_dir': _fwd(allow.get('data_dir'))},
               bench=b, recommend={
                   'ram_budget_gb': round(ram / 1e9, 2),
                   'log_ram_max_mb_s': round(min(live, logger.CEILING_BPS / 1e6), 1),
                   'log_disk_max_mb_s': round(min(live, logger.CEILING_BPS / 1e6, 0.7 * w), 1),
                   'ram_window_s': {'%g MB/s' % x: round(ram / (x * 1e6), 1) for x in (0.1, 1, 10, 50, 150)},
                   'note': 'short logs: --storage ram (default); disk only for records needed in full'},
               file=_fwd(host.HOST_FILE))
    if not allow.get('ram_bytes'):
        res['hint'] = 'ask the owner how much RAM / disk the logs may use and record it: openm454 bench --ram 8GB --disk 50GB'
    _emit(res)


def cmd_awg(args, scope):
    from .units import si
    if args.awg_cmd == 'off':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_off()}); return
    if args.awg_cmd in ('sweep', 'burst', 'mod'):
        lv = dict(high=si(args.high, 'V', '--high'), low=si(args.low, 'V', '--low'), duty=si(args.duty, None, '--duty'))
        if args.awg_cmd == 'sweep':
            st = scope.awg_sweep(si(args.start, 'Hz', '--start'), si(args.end, 'Hz', '--end'), si(args.time, 's', '--time'),
                                 'log' if args.log else 'linear', args.wave, **lv)
        elif args.awg_cmd == 'burst':
            st = scope.awg_burst(args.wave, si(args.freq, 'Hz', '--freq'), lv['high'], lv['low'], args.cycles,
                                 si(args.period, 's', '--period'), lv['duty'])
        else:
            st = scope.awg_modulate(args.kind, args.wave, si(args.freq, 'Hz', '--freq'), lv['high'], lv['low'],
                                    args.mod_wave, si(args.mod_freq, 'Hz', '--mod-freq'), si(args.depth, None, '--depth'),
                                    si(args.deviation, 'Hz', '--deviation') if args.deviation else None,
                                    si(args.hop, 'Hz', '--hop') if args.hop else None, lv['duty'])
        _emit({'ok': True, 'cmd': 'awg', 'awg': st, 'persists_after_exit': True})
        return
    if args.awg_cmd == 'dc':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_dc(si(args.level, 'V', '--level')), 'persists_after_exit': True})
        return
    if args.awg_cmd == 'status':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_status(), 'note': 'last state set by openm454 (device is write-only)'}); return
    if args.vpp is not None:
        vpp = si(args.vpp, 'V', '--vpp'); dc = si(args.dc, 'V', '--dc') if args.dc else vpp / 2
        high, low = dc + vpp / 2, dc - vpp / 2
    else:
        high = si(args.high, 'V', '--high') if args.high else 3.0
        low = si(args.low, 'V', '--low') if args.low else 0.0
    st = scope.awg_set(args.wave, si(args.freq, 'Hz', '--freq'), high, low, si(args.duty, None, '--duty'), si(args.phase, None, '--phase'))
    _emit({'ok': True, 'cmd': 'awg', 'awg': st, 'persists_after_exit': True})


def cmd_offline(args):
    from .capture import load_capture
    from .units import window, channel_list
    from . import plot as plotmod
    cap = load_capture(args.file)
    win = window(args.window)
    chans = None
    if getattr(args, 'channels', None):
        a, d = channel_list(args.channels); chans = a + d
    if args.cmd == 'plot':
        out = args.out or os.path.splitext(args.file)[0] + ('_zoom.png' if win else '.png')
        _emit({'ok': True, 'cmd': 'plot', 'files': {'png': _fwd(plotmod.plot(cap, out, window=win, channels=chans, title=os.path.basename(args.file)))}})
    else:
        res = {'ok': True, 'cmd': 'analyze', 'applied': cap.meta.get('applied'), 'acq': cap.meta.get('acq')}
        res.update(_measurements(cap, args, win, chans))
        if args.dump:
            res['dump'] = {ch: cap.dump(ch, args.dump, win) for ch in (chans or cap.channels())}
        _decode(cap, args, win, res)
        _emit(res)


def _budget(args):
    """-> (seconds, per_iteration or None). The watchdog only has to catch hangs, so this is a generous upper
    bound; for watch it bounds each capture (re-armed after every capture) instead of the whole loop.
    Compiling the settings offline here also rejects out-of-range settings before the device is opened."""
    from .scope import Scope, settings_from_args
    from .units import si
    if args.cmd == 'selftest':
        return 1200.0, None
    if args.cmd == 'bench':
        return 180.0, None
    if args.cmd == 'log':
        st, pl = log_plan(args, defer=True)            # rejects what cannot work before the device is opened
        _log_hook(args)
        per = max(60.0, 3 * pl['every_s'] + 60.0)        # re-armed after every statistics line
        return 90.0 + per, per
    if args.cmd not in ('capture', 'watch', 'stream'):
        return 60.0, None
    chained = False
    if args.cmd == 'stream':
        st, total, rec_s, chained = stream_plan(args)
    else:
        st = settings_from_args(args)
    from .decode import check
    check(args.decode, list(st.analog) + list(st.digital))
    if args.cmd == 'watch':
        _watch_fields(args)
    geom = Scope().plan(st)['geometry']
    rec = geom['record_s']
    collect = max(10.0, 1.5 * rec + 2.0)
    if args.cmd == 'stream':
        per = 5.0 + 2.5 * rec + 5.0 + 60.0 + 2e-8 * geom.get('acqu_bytes', 0)
        return (per, per) if chained else (per, None)       # a refreshing stream re-arms after every record
    wait = (st.timeout if st.trig_mode == 'normal' else (st.auto_after if st.trig_mode == 'auto' else 2.0)) + max(st.holdoff, 0.0)
    per = wait + rec + 0.5 + collect + 5.0
    if st.trig_mode == 'auto' and st.trig_source:      # forced fallback run: validate it too
        import copy
        fb = copy.copy(st)
        fb.trig_mode = 'force'
        rec2 = Scope().plan(fb)['geometry']['record_s']
        per += 2.5 + rec2 + max(10.0, 1.5 * rec2 + 2.0)
    if (st.trig_source or '').startswith('D') and st.trig_mode != 'force' and \
            st.trig_edge in ('rise', 'rising', 'fall', 'falling'):   # misfire re-arms: one more run each
        per += max(0, st.trig_retries) * (rec + 0.5 + collect + 1.0 + 2e-7 * geom.get('acqu_bytes', 0))
    per += 2e-7 * geom.get('acqu_bytes', 0)            # decoding and the hint checks of deep records
    if args.measure == 'full':
        per += 2e-7 * geom.get('acqu_bytes', 0)        # a second measuring pass, with the edge lists
    if args.cmd == 'capture':
        return max(60.0, per + 30.0), None
    per_iter = per + si(args.every, 's', '--every') + 30.0
    return per_iter, per_iter


def _watchdog_fire(cmd, max_wall, scope, done, wait=5.0):
    """Take over the output, report, stop and exit. Nothing here may block for long: a reader that stopped draining
    stdout leaves the main thread stuck inside _emit (holding the output lock) and a pipe that accepts no more
    writes, and the device must be released anyway (found by the hot-unplug test, 2026-10-31)."""
    if _OUT['lock'].acquire(timeout=wait):
        try:
            if done.is_set():
                return
            _OUT['owner'] = 'watchdog'
        finally:
            _OUT['lock'].release()
    else:
        _OUT['owner'] = 'watchdog'
    msg = {'ok': False, 'cmd': cmd, 'error': {
        'code': 'TIMEOUT_WATCHDOG', 'message': 'command exceeded its time budget (%.0f s, --max-wall); capture '
                                               'stopped and device released' % max_wall}}
    out = threading.Thread(target=_emit, args=(msg,), kwargs={'owner': 'watchdog'}, daemon=True)
    out.start()
    out.join(wait)
    helper = threading.Thread(target=scope.emergency_stop, daemon=True)
    helper.start()
    helper.join(10.0)            # never wait for a hung close: the OS releases the handle at exit
    os._exit(6)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd in ('plot', 'analyze'):
        try:
            cmd_offline(args)
            return 0
        except DscopeError as e:
            _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()}); return e.exit_code
        except Exception as e:
            _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e)}}); return 1
    if args.cmd == 'setup':
        from . import firmware
        try:
            d = firmware.save_dswave(args.dswave) if args.dswave else firmware.status(verify=True)
            for k in ('cache', 'dswave', 'saved'):
                if isinstance(d.get(k), str):
                    d[k] = _fwd(d[k])
            _emit({'ok': True, 'cmd': 'setup', **d})
            return 0
        except DscopeError as e:
            _emit({'ok': False, 'cmd': 'setup', 'error': e.to_dict()}); return e.exit_code
        except OSError as e:
            _emit({'ok': False, 'cmd': 'setup', 'error': {
                'code': 'INVALID_ARG', 'message': 'could not read DSWave or write the data folder: %s' % e,
                'hint': 'check the DSWave folder and that the data folder (OPENM454_HOME) is writable'}})
            return 2
    if args.cmd == 'stop':
        os.makedirs(os.path.dirname(STOP_REQUEST), exist_ok=True)
        with open(STOP_REQUEST, 'w') as f:
            f.write(str(time.time()))
        _emit({'ok': True, 'cmd': 'stop', 'note': 'a running watch/stream/log stops after its current capture or block'})
        return 0
    from .units import si
    from .scope import Scope
    try:                                     # everything here is offline: nothing has been sent to the scope yet
        if args.cmd == 'bench' and args.no_device:
            cmd_bench(args, None)
            return 0
        if args.cmd == 'log' and args.plan:
            from . import logger
            st, pl = log_plan(args, defer=True)
            _log_hook(args)                          # a broken hook file is refused here too
            _emit({'ok': True, 'cmd': 'log', 'plan': logger.public(pl)})
            return 0
        if args.cmd == 'debug' and args.debug_cmd == 'plan':
            from .scope import settings_from_args
            _emit({'ok': True, 'cmd': 'plan', **Scope().plan(settings_from_args(args))})
            return 0
        max_wall, per_iter = _budget(args)
        if args.max_wall:                    # an explicit value extends the computed budget, never shortens it
            given = si(args.max_wall, 's', '--max-wall')
            max_wall = max(max_wall, given)
            per_iter = max(per_iter, given) if per_iter else None
    except DscopeError as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()})
        return e.exit_code
    except Exception as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                         'trace': traceback.format_exc().splitlines()[-6:]}})
        return 1
    scope = Scope(log=(lambda s: sys.stderr.write(s + '\n')) if args.verbose else None)
    done = threading.Event()
    deadline = [time.time() + max_wall]
    if per_iter:
        _HEARTBEAT[0] = lambda: deadline.__setitem__(0, max(deadline[0], time.time() + per_iter))
    _IDLE[0] = lambda n: deadline.__setitem__(0, max(deadline[0], time.time() + 120.0 + 1e-6 * n))

    def watchdog():
        while True:
            left = deadline[0] - time.time()
            if left <= 0:
                break
            if done.wait(min(left, 1.0)):
                return
        _watchdog_fire(args.cmd, max_wall, scope, done)
    threading.Thread(target=watchdog, daemon=True).start()

    code = 0
    stale = False
    try:
        scope.open(reload_fpga=args.cmd == 'recover')
        if args.cmd in ('watch', 'stream', 'log'):   # once this process holds the scope (a BUSY one must not clear
            _clear_stop_request()                    # a request meant for the run that holds it)
        try:
            if args.cmd == 'info':
                _emit({'ok': True, 'cmd': 'info', **scope.info()})
            elif args.cmd == 'capture':
                cmd_capture(args, scope)
            elif args.cmd == 'watch':
                cmd_watch(args, scope)
            elif args.cmd == 'stream':
                cmd_stream(args, scope)
            elif args.cmd == 'log':
                code = cmd_log(args, scope)
            elif args.cmd == 'bench':
                cmd_bench(args, scope)
            elif args.cmd == 'awg':
                cmd_awg(args, scope)
            elif args.cmd == 'release':
                _emit({'ok': True, 'cmd': 'release', 'note': 'device opened, any left-over run stopped, idled and released'})
            elif args.cmd == 'recover':
                _emit({'ok': True, 'cmd': 'recover', 'device': scope.info()['device'],
                       'note': 'FPGA reset and reloaded; the signal generator is not reset by this (use awg off)'})
            elif args.cmd == 'selftest':
                from .selftest import run as selftest
                from .fixture import STANDARD
                res = selftest(scope, quick=args.quick, awg=not args.no_awg,
                               fx=STANDARD if args.fixture == 'standard' else None)
                _emit(res)
                code = 0 if res.get('ok') else 1
            elif args.cmd == 'debug':
                _emit({'ok': True, 'cmd': 'debug', **scope.debug(args)})
        finally:
            dev = scope.dev
            stale = dev is not None and dev.t.stale
            scope.close()
    except DscopeError as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()})
        code = e.exit_code
    except KeyboardInterrupt:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERRUPTED', 'message': 'interrupted (any capture '
                                                                                         'was stopped first)'}})
        code = 1
    except Exception as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                         'trace': traceback.format_exc().splitlines()[-6:]}})
        code = 1
    finally:
        with _OUT['lock']:
            done.set()
            watchdog_owns = _OUT['owner'] == 'watchdog'
        if stale and not watchdog_owns:
            sys.stdout.flush()
            os._exit(code)           # libusb finalizers would wait for the stale transfers forever
    return code


if __name__ == '__main__':
    sys.exit(main())
