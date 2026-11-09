"""Calibrated, read-only observation of visible source handles, not a writer lock.

The existing source FD is a positive control. Empty/partial/error output cannot
establish release. Raw lsof output stays private; only aggregate evidence escapes.
No observation rules out invisible processes or future opens.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import hashlib
import json
import re
import stat
import subprocess
import sys
import time

QUIESCENCE_SECONDS = 2.0
SCAN_TIMEOUT_SECONDS = 10


def _scan(path, package, descriptor):
    identity = os.fstat(descriptor)
    command = ['lsof', '-nP', '-F0pfDitn']
    # Never allow a Linux lsof device-cache refresh to write into HOME. Older
    # builds without this option fail closed; macOS has no -D option.
    if sys.platform.startswith('linux'):
        command.append('-Di')
    command += ['+D', str(path)] if package else ['--', str(path)]
    try:
        result = subprocess.run(command, shell=False, capture_output=True, timeout=SCAN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return dict(status='unknown', control_seen=False, other_handles=0)
    unknown = dict(status='unknown', control_seen=False, other_handles=0)
    # macOS +D returns 1 when the selected directory itself has no handles,
    # even with matching descendant FDs. Neither 0 nor 1 proves anything: the
    # exact same-scope positive control below is mandatory in both cases.
    if result.returncode not in (0, 1) or result.stderr or not result.stdout or len(result.stdout) > 8 * 1024 * 1024:
        return unknown
    # -F0 ends each field in NUL and each process/file set in exactly one
    # LF. Split on NUL only: literal LF inside a name value is not a boundary.
    pid, record, control, others = None, None, 0, 0

    def finish():
        nonlocal control, others
        if record is None:
            return
        if set(record) != {b'f', b'D', b'i', b't', b'n'}:
            raise ValueError('incomplete file set')
        fd = record[b'f']
        if not record[b't'] or not record[b'n'] or not re.fullmatch(rb'[0-9]+', record[b'i']) or not re.fullmatch(rb'0x[0-9a-fA-F]+', record[b'D']):
            raise ValueError('invalid file set')
        is_control = (pid == os.getpid() and fd == str(descriptor).encode() and
                      int(record[b'D'], 16) == identity.st_dev and int(record[b'i']) == identity.st_ino and record[b't'] == b'REG')
        if is_control:
            control += 1
        else:
            others += 1

    try:
        fields = result.stdout.split(b'\x00')
        if len(fields) < 2 or fields[-1] != b'\n':
            return unknown
        for index, field in enumerate(fields[:-1]):
            boundary = field.startswith(b'\n')
            if boundary:
                field = field[1:]
            if not field:
                return unknown
            tag, value = field[:1], field[1:]
            if index == 0:
                if boundary or tag != b'p':
                    return unknown
            elif boundary != (tag in (b'p', b'f')):
                # New process/file sets REQUIRE their predecessor's LF. A LF
                # between ordinary fields, or repeated LF, is malformed too.
                return unknown
            if tag != b'n' and b'\n' in value:
                return unknown
            if tag == b'p':
                if index and record is None:
                    return unknown
                finish()
                record = None
                if not re.fullmatch(rb'[1-9][0-9]*', value):
                    return unknown
                pid = int(value)
            elif tag == b'f':
                finish()
                if pid is None or not value:
                    return unknown
                record = {tag: value}
            elif tag in (b'D', b'i', b't', b'n') and record is not None and tag not in record:
                record[tag] = value
            else:
                return unknown
        if record is None:
            return unknown
        finish()
    except (ValueError, OverflowError):
        return unknown
    if control != 1:
        return unknown
    return dict(status='busy' if others else 'observed-released', control_seen=True, other_handles=others)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def observe(path, *, package, snapshot, between_scans=None):
    """Two calibrated scans plus unchanged caller-supplied native fingerprints.

    snapshot() must validate terminal evidence and identities as well as return
    a deterministic, content-bound inventory. Exceptions deliberately fail closed.
    The control FD is never inherited and is closed on every return path.
    """
    proof = dict(version=1, method='lsof-positive-control-v1', scope='package-tree' if package else 'exact-file',
                 status='unknown', started_at=datetime.now(timezone.utc).isoformat(), ended_at='', duration_ms=0, scans=[])
    started = time.monotonic()
    descriptor = None
    try:
        before = snapshot()
        proof['inventory_sha256'] = fingerprint(before)
        control = path / 'meta.json' if package else path
        descriptor = os.open(control, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode):
            return proof
        for index in range(2):
            current = os.stat(control, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino) or snapshot() != before:
                return proof
            scan = _scan(path, package, descriptor)
            scan['observed_at'] = datetime.now(timezone.utc).isoformat()
            proof['scans'].append(scan)
            if scan['status'] != 'observed-released':
                proof['status'] = scan['status']
                return proof
            if snapshot() != before:
                return proof
            if index == 0:
                if between_scans:
                    between_scans()
                time.sleep(QUIESCENCE_SECONDS)
        proof['status'] = 'observed-released'
        return proof
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return proof
    finally:
        if descriptor is not None:
            os.close(descriptor)
        proof['ended_at'] = datetime.now(timezone.utc).isoformat()
        proof['duration_ms'] = int((time.monotonic() - started) * 1000)
