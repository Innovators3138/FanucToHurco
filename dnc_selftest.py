#!/usr/bin/env python3
"""
dnc_selftest.py  —  exercise every qsdnc operation against a real device.

    python3 dnc_selftest.py 192.168.30.10 [trace.log]

Works only inside a scratch folder (TEST_DIR) that it creates and removes, and
refuses to start if that folder already exists or the device is feeding a
program.  Never sends RunFile or StopDnc.  Every packet is written to the trace
log so a failure comes with the raw bytes that caused it.
"""

import os
import random
import sys
import time
from typing import Callable, List, Tuple

import qsdnc
from qsdnc import QSClient, DncError, FatFsError

TEST_DIR = '\\cltest'

# Sizes chosen around the 512-byte block edges, plus one bigger than any
# program on the device so far.
SIZES = [1, 100, 511, 512, 513, 1024, 1500, 65543, 300000]


class TracingClient(QSClient):
    def __init__(self, ip: str, log):
        super().__init__(ip)
        self.log = log
        self.t0 = time.time()

    def _line(self, arrow: str, raw: bytes) -> None:
        shown = raw if len(raw) <= 40 else raw[:40]
        more = f' ... (+{len(raw) - 40} bytes)' if len(raw) > 40 else ''
        self.log.write(f'{time.time() - self.t0:9.3f} {arrow} {shown.hex(" ")}{more}\n')

    def _send(self, pkt: bytes) -> None:
        self._line('->', pkt)
        super()._send(pkt)

    def _recv(self):
        result = super()._recv()
        self._line('<-', result[3])
        return result


def gcode_like(size: int, seed: int) -> bytes:
    """Printable G-code-ish lines with CRLF, cut to exactly `size` bytes."""
    rng = random.Random(seed)
    out = bytearray(b'%\r\n')
    n = 2
    while len(out) < size:
        out += (f'N{n}G01X{rng.uniform(-9, 9):.4f}Y{rng.uniform(-9, 9):.4f}'
                f'F{rng.randint(1, 60)}.\r\n').encode('ascii')
        n += 2
    return bytes(out[:size])


def all_bytes(size: int) -> bytes:
    """Every byte value 0-255, to catch any text translation on the device."""
    return bytes(i % 256 for i in range(size))


class Runner:
    def __init__(self, client: TracingClient):
        self.c = client
        self.results: List[Tuple[str, str, str]] = []

    def step(self, name: str, fn: Callable[[], str]) -> bool:
        self.c.log.write(f'\n=== {name}\n')
        try:
            detail = fn() or ''
            self.results.append(('PASS', name, detail))
            print(f'  PASS  {name}  {detail}')
            return True
        except AssertionError as exc:
            self.results.append(('FAIL', name, str(exc)))
            print(f'  FAIL  {name}  {exc}')
        except DncError as exc:
            self.results.append(('FAIL', name, f'{type(exc).__name__}: {exc}'))
            print(f'  FAIL  {name}  {type(exc).__name__}: {exc}')
        return False

    def info(self, name: str, fn: Callable[[], str]) -> None:
        """A probe whose outcome is worth knowing but has no right answer yet."""
        self.c.log.write(f'\n=== {name} (info)\n')
        try:
            detail = fn()
        except DncError as exc:
            detail = f'{type(exc).__name__}: {exc}'
        self.results.append(('INFO', name, detail))
        print(f'  INFO  {name}  {detail}')

    # ── helpers ──────────────────────────────────────────────────────────────
    def listed(self, path: str):
        return {e.name: e for e in self.c.list_dir(path)}

    def round_trip(self, name: str, data: bytes) -> str:
        path = qsdnc.join(TEST_DIR, name)
        t = time.time()
        self.c.upload(path, data)
        up = time.time() - t
        entry = self.listed(TEST_DIR).get(name)
        assert entry is not None, f'{name} not in listing after upload'
        assert entry.size == len(data), \
            f'listing says {entry.size} bytes, uploaded {len(data)}'
        t = time.time()
        back = self.c.download(path, entry.size)
        down = time.time() - t
        if back != data:
            first = next((i for i, (a, b) in enumerate(zip(back, data)) if a != b),
                         min(len(back), len(data)))
            raise AssertionError(f'download differs: got {len(back)} bytes, '
                                 f'first mismatch at offset {first}')
        rate = len(data) / 1024 / max(up + down, 1e-6)
        return f'up {up:.2f}s  down {down:.2f}s  ({rate:.0f} KiB/s both ways)'

    def expect_fr(self, fn: Callable[[], None], codes) -> str:
        try:
            fn()
        except FatFsError as exc:
            assert exc.code in codes, f'got {exc}, expected FR code in {codes}'
            return str(exc)
        raise AssertionError('device accepted it')


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    ip = argv[1]
    log_path = argv[2] if len(argv) > 2 else 'dnc_selftest.log'
    with open(log_path, 'w') as log, TracingClient(ip, log) as c:
        print(f'device {ip}, local port {c.local_port}, trace -> {log_path}')
        print(f'  {c.device_info()}')
        st = c.read_status()
        print(f'  {st.mode_text} / {st.process_text}  {st.current_file}')
        if st.process == 2:
            print('device is running a program; not testing')
            return 1
        root_before = c.list_dir()
        if any(e.name.lower() == TEST_DIR.strip('\\').lower() for e in root_before):
            print(f'{TEST_DIR} already exists on the device; remove it or '
                  'change TEST_DIR')
            return 1

        r = Runner(c)
        if not r.step(f'mkdir {TEST_DIR}', lambda: c.mkdir(TEST_DIR)):
            return 1
        try:
            r.step('new folder is listed as a folder',
                   lambda: _assert(r.listed('').get(TEST_DIR[1:]) is not None
                                   and r.listed('')[TEST_DIR[1:]].is_dir,
                                   'not in root listing as <DIR>'))
            r.step('new folder lists empty',
                   lambda: _assert(r.listed(TEST_DIR) == {}, 'not empty'))

            for size in SIZES:
                r.step(f'round trip {size} bytes',
                       lambda s=size: r.round_trip(f'T{s}.NC', gcode_like(s, s)))
            r.step('round trip every byte value (2048 bytes)',
                   lambda: r.round_trip('BIN.NC', all_bytes(2048)))
            r.step('long name with spaces',
                   lambda: r.round_trip('Long name test.nc', gcode_like(900, 7)))
            r.step('overwrite with a shorter file leaves no stale tail',
                   lambda: r.round_trip('T1500.NC', gcode_like(700, 99)))
            r.info('zero-byte upload',
                   lambda: r.round_trip('EMPTY.NC', b'') or 'round-tripped')

            def listing_complete() -> str:
                names = set(r.listed(TEST_DIR))
                want = {f'T{s}.NC' for s in SIZES} | {'BIN.NC', 'Long name test.nc'}
                missing = want - names
                assert not missing, f'missing from listing: {sorted(missing)}'
                return f'{len(names)} entries'
            r.step('listing returns every file', listing_complete)

            def rename() -> str:
                old, new = qsdnc.join(TEST_DIR, 'T512.NC'), qsdnc.join(TEST_DIR, 'R512.NC')
                c.rename(old, new)
                names = r.listed(TEST_DIR)
                assert 'T512.NC' not in names, 'old name still listed'
                assert 'R512.NC' in names, 'new name not listed'
                assert c.download(new, names['R512.NC'].size) == gcode_like(512, 512), \
                    'renamed file content changed'
                return ''
            r.step('rename keeps content', rename)

            r.step('download of a missing file fails cleanly',
                   lambda: _raises(lambda: c.download_path(
                       qsdnc.join(TEST_DIR, 'NOPE.NC'))))
            r.step('delete of a missing file reports an FR code',
                   lambda: r.expect_fr(lambda: c.delete_file(
                       qsdnc.join(TEST_DIR, 'NOPE.NC')), (4, 5)))
            r.step('mkdir of an existing folder reports FR_EXIST',
                   lambda: r.expect_fr(lambda: c.mkdir(TEST_DIR), (8,)))
            r.info('delete of a non-empty folder',
                   lambda: r.expect_fr(lambda: c.delete_folder(TEST_DIR), (7,)))
        finally:
            print('cleanup')
            try:
                for e in c.list_dir(TEST_DIR):
                    r.step(f'delete {e.name}', lambda n=e.name:
                           c.delete_file(qsdnc.join(TEST_DIR, n)))
            except DncError as exc:
                print(f'  could not list {TEST_DIR} for cleanup: {exc}')
            r.step(f'delete folder {TEST_DIR}', lambda: c.delete_folder(TEST_DIR))
            r.step('root listing is back to how it was',
                   lambda: _assert([(e.name, e.size) for e in c.list_dir()] ==
                                   [(e.name, e.size) for e in root_before],
                                   'root listing changed'))
        for note in c.notices:
            print(f'  device: {note}')

    fails = [x for x in r.results if x[0] == 'FAIL']
    print(f'\n{len(r.results) - len(fails)} ok, {len(fails)} failed; '
          f'trace in {os.path.abspath(log_path)}')
    return 1 if fails else 0


def _assert(cond: bool, msg: str) -> str:
    assert cond, msg
    return ''


def _raises(fn: Callable[[], object]) -> str:
    try:
        fn()
    except DncError as exc:
        return f'{type(exc).__name__}: {exc}'
    raise AssertionError('no error raised')


if __name__ == '__main__':
    sys.exit(main(sys.argv))
