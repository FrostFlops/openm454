"""The full usb_ceiling entry point against a stand-in scope: a name in main() used to shadow the trace capture.

The review caught it: main() bound the output path to `transfers`, so `trace = transfers(scope)` raised TypeError after
the ceiling CSV had been written, and every run without --quick lost its trace. Everything here is offline.
"""
import csv
import json

import pytest

from tests.hardware.research import usb_ceiling as script

TRACE = [(0.0, 1024 * 1024, 16), (0.1005, 1024 * 1024, 15)]


CREATED = []


class _Scope:
    """Counts what a bench-safety path has to do: park the generator and close the device."""

    def __init__(self):
        self.parked = 0
        self.closed = 0
        CREATED.append(self)

    def open(self):
        pass

    def close(self):
        self.closed += 1

    def awg_status(self):
        return None

    def awg_off(self):
        self.parked += 1

    def info(self):
        return {'device': {'model': 'stand-in'}}


class _Link:
    def program(self, scope):
        pass


class _Bench:
    def link(self, name):
        return _Link()


def run(tmp_path, monkeypatch, quick=False):
    monkeypatch.setattr(script.scopemod, 'Scope', _Scope)
    monkeypatch.setattr(script.benchmod, 'Bench', lambda declaration: _Bench())
    monkeypatch.setattr(script, 'matrix', lambda scope, seconds: [{'config': '1A 400M', 'host_mb_s': 287.0}])
    calls = []

    def transfers(scope):
        calls.append(scope)
        return list(TRACE)
    monkeypatch.setattr(script, 'transfers', transfers)
    argv = ['--bench-confirmed', '--out-dir', str(tmp_path)] + (['--quick'] if quick else [])
    return script.main(argv), calls


def manifest(tmp_path):
    return json.loads((tmp_path / 'manifest-usb_ceiling.json').read_text(encoding='utf-8'))


def test_a_full_run_captures_the_trace_and_writes_both_files(tmp_path, monkeypatch):
    code, calls = run(tmp_path, monkeypatch)
    assert code == 0 and len(calls) == 1, 'the trace has to be captured, not skipped'
    assert (tmp_path / 'usb-ceiling.csv').exists() and (tmp_path / 'usb-transfers.csv').exists()
    rows = list(csv.DictReader((tmp_path / 'usb-transfers.csv').open(encoding='utf-8')))
    assert len(rows) == 2 and rows[1]['bytes'] == str(1024 * 1024) and rows[1]['t_s'] == str(round(0.1005, 6))
    doc = manifest(tmp_path)
    assert doc['files'] == ['usb-ceiling.csv', 'usb-transfers.csv'] and doc['stale'] == []


def test_a_failing_manifest_still_parks_the_generator_and_closes_the_scope(tmp_path, monkeypatch):
    """The review's P1: the manifest used to be written before the cleanup in the same finally block, so a full disk
    left the generator driving the declared link. A file that cannot be written must not keep the bench live."""
    monkeypatch.setattr(script.benchmod, 'write_manifest',
                        lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
    code, calls = run(tmp_path, monkeypatch)
    assert code == 1, 'no manifest: the run is not a success'
    assert len(calls) == 1 and (tmp_path / 'usb-ceiling.csv').exists(), 'the data is written before the manifest'
    scope = CREATED[-1]
    assert scope.parked == 1 and scope.closed == 1


def test_a_new_out_dir_is_created_before_the_run(tmp_path, monkeypatch):
    """The review's P2: the CSVs were written before anything created the directory, so a first run into a new
    --out-dir failed after the captures (only the manifest was left, because that is what creates the directory)."""
    out = tmp_path / 'fresh' / 'nested'
    code, _calls = run(out, monkeypatch)
    assert code == 0
    assert (out / 'usb-ceiling.csv').exists() and (out / 'usb-transfers.csv').exists()
    assert (out / 'manifest-usb_ceiling.json').exists()


def test_a_quick_run_skips_the_trace_and_marks_the_old_one_stale(tmp_path, monkeypatch):
    (tmp_path / 'usb-transfers.csv').write_text('t_s,bytes,inflight\n', encoding='utf-8')   # an earlier run
    code, calls = run(tmp_path, monkeypatch, quick=True)
    assert code == 0 and calls == []
    doc = manifest(tmp_path)
    assert doc['files'] == ['usb-ceiling.csv'] and doc['stale'] == ['usb-transfers.csv']
