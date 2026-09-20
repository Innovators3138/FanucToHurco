#!/usr/bin/env python3
"""
qsdnc.py  —  Micro DNC / QS Explorer 4.06 wire protocol client.

A TFTP-shaped UDP protocol, but NOT real TFTP:
  * 4-byte block numbers instead of TFTP's 2-byte ones
  * both ends use port 69 (no ephemeral TID negotiation)
  * no mode field, no authentication, one client at a time

Deliberately Qt-free and blocking, so it can be exercised from a plain REPL or
the CLI at the bottom of this file:

    python3 qsdnc.py 192.168.1.50 info
    python3 qsdnc.py 192.168.1.50 ls "\\PROGRAMS"
    python3 qsdnc.py 192.168.1.50 get "\\PROGRAMS\\PART1.NC" part1.nc
    python3 qsdnc.py 192.168.1.50 put part1.nc "\\PROGRAMS\\PART1.NC"
    python3 qsdnc.py 192.168.1.50 run "\\PROGRAMS\\PART1.NC"
"""

import math
import socket
import struct
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

# ─────────────────────────── transport constants ────────────────────────────
PORT = 69                # destination port on the device
BIND_PORT = 69           # local source port; needs root, falls back to ephemeral
TIMEOUT = 0.8            # Socket.ReceiveTimeout = 0x320 in the stock client
CMD_RETRIES = 5
LONG_CMD_RETRIES = 10    # WRQ / CloseFile in the copy task
DATA_RETRIES = 10        # DATA_RESEND_MAX
BLOCK = 512
BIND_RETRIES = 5

# ───────────────────────────── client → device ──────────────────────────────
OP_RRQ      = 0x01
OP_WRQ      = 0x02
OP_DATA     = 0x03
OP_ACK      = 0x04
OP_READDIR  = 0x08
OP_MKDIR    = 0x0A
OP_DELFILE  = 0x0C
OP_DELDIR   = 0x0E
OP_RENAME   = 0x10
OP_INFO     = 0x13
OP_MSG      = 0x15
OP_RUN      = 0x19
OP_OPENDIR  = 0x1B
OP_STOPDNC  = 0x1C
OP_STATUS   = 0x32
OP_CLOSE    = 0x34
OP_DLRQ     = 0x36
OP_DLDATA   = 0x38

# ───────────────────────────── device → client ──────────────────────────────
R_DATA        = 0x03
R_DATAACK     = 0x04
R_ERROR       = 0x05
R_OPENDIRACK  = 0x07
R_READDIR     = 0x09
R_MKDIRACK    = 0x0B
R_DELFILEACK  = 0x0D
R_DELDIRACK   = 0x0F
R_RENAMEACK   = 0x11
R_WAIT        = 0x12
R_INFOACK     = 0x14
R_MSGACK      = 0x16
R_STARTUPCOPY = 0x18
R_RUNACK      = 0x1A
R_DNCMODE     = 0x1B
R_DNCSTOPACK  = 0x1D
R_STATUSACK   = 0x33
R_CLOSEACK    = 0x35
R_DLRQACK     = 0x37
R_DLDATAACK   = 0x39
R_BUSY        = 0x63

# Packets the device pushes on its own; they can land in the middle of any
# exchange, so every wait loop steps over them.
UNSOLICITED = (R_DNCMODE, R_STARTUPCOPY)

# ────────────────────────────── FatFs results ───────────────────────────────
FR_OK = 0
FR_NO_FILE = 4
FR_TEXT = {
    0:  'OK',
    1:  'disk error',
    2:  'internal error',
    3:  'drive not ready',
    4:  'no such file',
    5:  'no such path',
    6:  'invalid name',
    7:  'access denied',
    8:  'already exists',
    9:  'invalid object',
    10: 'write protected',
    11: 'invalid drive',
    12: 'not enabled',
    13: 'no filesystem',
    14: 'mkfs aborted',
    15: 'timeout',
}

# FatFs attribute bits
AM_DIR = 0x10

# Device status enumerations
DEVICE_MODES = {0: 'Explorer', 1: 'About', 2: 'Settings',
                3: 'Simulation', 4: 'Edit', 5: 'Read', 6: 'DNC'}
PROCESS_STATES = {0: 'Stop', 1: 'Waiting', 2: 'Running'}


# ────────────────────────────────── errors ──────────────────────────────────
class DncError(Exception):
    """Base class for every failure this module raises."""


class DncTimeout(DncError):
    """The device stopped answering."""


class DncAborted(DncError):
    """The caller asked to stop while a call was in its retries."""


class DncBusy(DncError):
    """Opcode 0x63 — another PC already owns this device."""


class DncRemoteError(DncError):
    """Opcode 0x05 — the device sent a TFTP-style error packet."""


class FatFsError(DncError):
    """A filesystem operation came back with a non-zero FR_* result code."""

    def __init__(self, code: int, what: str = ''):
        self.code = code
        text = FR_TEXT.get(code, f'FR code {code}')
        super().__init__(f'{what}: {text}' if what else text)


# ───────────────────────────── path helpers ─────────────────────────────────
# Device paths use '\' and no drive letter; the root is the empty string.
# The "0:" shown by the stock GUI is decoration only.
ROOT = ''


def join(parent: str, name: str) -> str:
    """Join a device directory and an entry name."""
    return (parent.rstrip('\\') + '\\' + name) if name else parent


def parent_of(path: str) -> str:
    """Containing directory of a device path, or ROOT."""
    cut = path.rstrip('\\').rfind('\\')
    return path[:cut] if cut > 0 else ROOT


def basename(path: str) -> str:
    return path.rstrip('\\').rsplit('\\', 1)[-1]


def _z(text: str) -> bytes:
    """NUL-terminated, one byte per character (the device is Latin-1/ASCII)."""
    return text.encode('latin-1', errors='replace') + b'\x00'


def _packet(op: int, payload: bytes = b'') -> bytes:
    """Opcode is 16-bit big-endian with a high byte that is always zero."""
    return bytes((0, op)) + payload


# ───────────────────────────── directory entry ──────────────────────────────
@dataclass
class Entry:
    name: str
    size: int
    attr: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & AM_DIR)


@dataclass
class Status:
    mode: int
    load_percent: int
    process: int
    cycle_minutes: int
    ip: str = ''
    current_file: str = ''

    @property
    def mode_text(self) -> str:
        return DEVICE_MODES.get(self.mode, f'mode {self.mode}')

    @property
    def process_text(self) -> str:
        return PROCESS_STATES.get(self.process, f'state {self.process}')


ProgressFn = Optional[Callable[[int, int], None]]


# ────────────────────────────────── client ──────────────────────────────────
class QSClient:
    """
    One conversation with one device.

    The device allows a single client at a time, so hold an instance only for
    as long as you need it and close it when done.
    """

    def __init__(self, ip: str, port: int = PORT, timeout: float = TIMEOUT,
                 bind_port: int = BIND_PORT,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.bind_port = bind_port
        self.sock: Optional[socket.socket] = None
        self.local_port: Optional[int] = None
        self.notices: List[str] = []   # unsolicited device pushes, newest last
        # A silent device costs retries * timeout seconds per call, so a caller
        # that wants to quit needs a way in between attempts.
        self.should_stop = should_stop

    # ── lifecycle ────────────────────────────────────────────────────────────
    def open(self) -> None:
        """
        Bind the local socket.

        The stock client binds source port 69.  That needs root on macOS and
        Linux, so fall back to an ephemeral port: the device answers whatever
        source port it sees, but the stock client never relies on that, so the
        caller is told which port it actually got via `local_port`.
        """
        if self.sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound = False
        for _attempt in range(BIND_RETRIES):
            try:
                sock.bind(('', self.bind_port))
                bound = True
                break
            except OSError:
                continue
        if not bound:
            try:
                sock.bind(('', 0))
            except OSError as exc:
                sock.close()
                raise DncError(f'cannot bind a local UDP port: {exc}') from exc
        sock.settimeout(self.timeout)
        self.sock = sock
        self.local_port = sock.getsockname()[1]   # what we got, not what we asked

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> 'QSClient':
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── raw I/O ──────────────────────────────────────────────────────────────
    def _require_sock(self) -> socket.socket:
        if self.sock is None:
            raise DncError('client is not open')
        return self.sock

    def _send(self, pkt: bytes) -> None:
        self._require_sock().sendto(pkt, (self.ip, self.port))

    def _recv(self) -> Tuple[int, int, bytes, bytes]:
        """One datagram -> (op, number, body-from-offset-6, raw)."""
        raw, _addr = self._require_sock().recvfrom(4096)
        if len(raw) < 5:
            raise DncError(f'malformed response ({len(raw)} bytes)')
        op = raw[1]
        number = int.from_bytes(raw[2:6], 'big') if len(raw) >= 6 else 0
        return op, number, raw[6:], raw

    def _drain(self) -> None:
        """
        Discard anything already queued.

        The stock client closes and re-opens its socket before every command
        for the same reason: stale or pushed datagrams must not be mistaken
        for the answer to the command about to be sent.
        """
        sock = self._require_sock()
        sock.setblocking(False)
        try:
            while True:
                try:
                    raw, _ = sock.recvfrom(4096)
                except (BlockingIOError, OSError):
                    break
                if len(raw) >= 2 and raw[1] in UNSOLICITED:
                    self._note(raw)
        finally:
            sock.settimeout(self.timeout)

    def _note(self, raw: bytes) -> None:
        text = raw[6:].split(b'\x00')[0].decode('latin-1', 'replace').strip()
        label = 'entered DNC mode' if raw[1] == R_DNCMODE else 'startup copy'
        self.notices.append(f'{label}: {text}' if text else label)

    # ── one request / one response ───────────────────────────────────────────
    def _command(self, op: int, payload: bytes, expect: Sequence[int],
                 retries: int = CMD_RETRIES, drain: bool = True
                 ) -> Tuple[int, int, bytes, bytes]:
        pkt = _packet(op, payload)
        if drain:
            self._drain()
        saw_wait = False
        for _attempt in range(retries + 1):
            self._abort_check()
            if not saw_wait:
                # After a WaitACK the device is already working on it; resending
                # a mutating command could run it twice.
                self._send(pkt)
            try:
                while True:
                    rop, number, body, raw = self._recv()
                    if rop == R_BUSY:
                        raise DncBusy('another PC is already connected '
                                      'to this device')
                    if rop == R_ERROR:
                        code = int.from_bytes(raw[2:4], 'big')
                        msg = raw[4:].split(b'\x00')[0].decode('latin-1', 'replace')
                        raise DncRemoteError(f'device error {code}: {msg}'
                                             if msg else f'device error {code}')
                    if rop in UNSOLICITED:
                        self._note(raw)
                        continue
                    if rop == R_WAIT:
                        saw_wait = True   # operation in progress; wait for more
                        continue
                    if rop in expect:
                        return rop, number, body, raw
                    # Anything else is a straggler from an earlier exchange.
                    continue
            except socket.timeout:
                continue
        raise DncTimeout(f'no response to opcode 0x{op:02X} from {self.ip}')

    def _abort_check(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise DncAborted('cancelled')

    @staticmethod
    def _check_fr(code: int, what: str) -> None:
        if code != FR_OK:
            raise FatFsError(code, what)

    # ── device info, messaging, DNC control ──────────────────────────────────
    def device_info(self) -> str:
        _op, _n, body, _raw = self._command(OP_INFO, b'\x00\x00', (R_INFOACK,))
        return body.split(b'\x00')[0].decode('latin-1', 'replace').strip()

    def read_status(self) -> Status:
        _op, _n, body, _raw = self._command(OP_STATUS, b'\x00\x00', (R_STATUSACK,))
        if len(body) < 5:
            raise DncError('short status record')
        mode, load, process = body[0], body[1], body[2]
        cycle = int.from_bytes(body[3:5], 'big')
        tail = body[5:].split(b'\x00')[0].decode('latin-1', 'replace')
        ip, _, current = tail.partition('|')
        return Status(mode, load, process, cycle, ip, current)

    def send_message(self, text: str) -> None:
        self._command(OP_MSG, _z(text), (R_MSGACK,))

    def run_file(self, path: str) -> None:
        """Start the device drip-feeding `path` to the machine."""
        self._command(OP_RUN, _z(path), (R_RUNACK,))

    def stop_dnc(self) -> None:
        self._command(OP_STOPDNC, b'\x00', (R_DNCSTOPACK,))

    # ── directory listing ────────────────────────────────────────────────────
    def list_dir(self, path: str = ROOT) -> List[Entry]:
        _op, code, _body, _raw = self._command(OP_OPENDIR, _z(path),
                                               (R_OPENDIRACK,))
        self._check_fr(code, f'open {path or chr(92)}')

        entries: List[Entry] = []
        # The device keeps the cursor; the index field in the request is always 0.
        request = b'\x00\x00' + _z(path)
        while True:
            _op, code, body, _raw = self._command(OP_READDIR, request,
                                                  (R_READDIR,))
            if code != FR_OK:
                if code == FR_NO_FILE:
                    break          # nothing more to list
                raise FatFsError(code, f'read {path or chr(92)}')
            entry = self._parse_entry(body)
            if entry is None:
                break              # index 0xFFFF terminates the listing
            if entry.name not in ('.', '..'):
                entries.append(entry)
            if len(entries) > 10000:
                raise DncError('directory listing did not terminate')
        entries.sort(key=lambda e: (not e.is_dir, e.name.upper()))
        return entries

    @staticmethod
    def _parse_entry(body: bytes) -> Optional[Entry]:
        """
        name\0 | u16 index | u8 attributes | u32 size
        Index 0xFFFF marks the end of the listing.
        """
        cut = body.find(b'\x00')
        if cut < 0:
            raise DncError('directory entry has no name terminator')
        tail = body[cut + 1:]
        if len(tail) < 7:
            raise DncError('directory entry is truncated')
        index = int.from_bytes(tail[0:2], 'big')
        if index == 0xFFFF:
            return None
        name = body[:cut].decode('latin-1', 'replace')
        return Entry(name, int.from_bytes(tail[3:7], 'big'), tail[2])

    # ── mutating filesystem operations ───────────────────────────────────────
    def mkdir(self, path: str) -> None:
        _op, code, _b, _r = self._command(OP_MKDIR, _z(path), (R_MKDIRACK,))
        self._check_fr(code, f'create folder {path}')

    def delete_file(self, path: str) -> None:
        _op, code, _b, _r = self._command(OP_DELFILE, _z(path), (R_DELFILEACK,))
        self._check_fr(code, f'delete {path}')

    def delete_folder(self, path: str) -> None:
        _op, code, _b, _r = self._command(OP_DELDIR, _z(path), (R_DELDIRACK,))
        self._check_fr(code, f'delete folder {path}')

    def rename(self, old: str, new: str) -> None:
        _op, code, _b, _r = self._command(OP_RENAME, _z(old) + _z(new),
                                          (R_RENAMEACK,))
        self._check_fr(code, f'rename {old}')

    def close_file(self) -> None:
        self._command(OP_CLOSE, b'0\x00', (R_CLOSEACK,), retries=LONG_CMD_RETRIES)

    # ── transfers ────────────────────────────────────────────────────────────
    def upload(self, path: str, data: bytes, progress: ProgressFn = None) -> int:
        """PC → device.  Returns the number of blocks sent."""
        self._command(OP_WRQ, _z(path), (R_DATAACK,), retries=LONG_CMD_RETRIES)

        total = math.ceil(len(data) / BLOCK)
        for block in range(1, total + 1):
            chunk = data[(block - 1) * BLOCK: block * BLOCK]
            self._send_block(block, chunk)
            if progress:
                progress(block, total)
        self.close_file()
        return total

    def _send_block(self, block: int, chunk: bytes) -> None:
        pkt = _packet(OP_DATA, struct.pack('>I', block) + chunk)
        for _attempt in range(DATA_RETRIES + 1):
            self._abort_check()
            self._send(pkt)
            try:
                while True:
                    rop, number, _body, raw = self._recv()
                    if rop == R_BUSY:
                        raise DncBusy('another PC took the device mid-transfer')
                    if rop == R_ERROR:
                        code = int.from_bytes(raw[2:4], 'big')
                        msg = raw[4:].split(b'\x00')[0].decode('latin-1', 'replace')
                        raise DncRemoteError(f'device error {code}: {msg}')
                    if rop in UNSOLICITED:
                        self._note(raw)
                        continue
                    if rop == R_WAIT:
                        continue
                    if rop == R_DATAACK:
                        if number == block:
                            return
                        break          # wrong block acked — resend this one
            except socket.timeout:
                continue
        raise DncTimeout(f'block {block} was never acknowledged')

    def cancel_transfer(self) -> None:
        """CANCEL_COPY_PACKET: a DATA packet for block 0 with no payload."""
        self._send(_packet(OP_DATA, struct.pack('>I', 0)))

    def download(self, path: str, size: int, progress: ProgressFn = None) -> bytes:
        """
        Device → PC.

        `size` comes from the directory listing: the protocol carries no EOF
        marker, so the block count has to be known up front.
        """
        self._command(OP_DLRQ, _z(path), (R_DLRQACK,), retries=LONG_CMD_RETRIES)

        total = math.ceil(size / BLOCK)
        out = bytearray()
        for block in range(1, total + 1):
            _op, number, body, _raw = self._command(
                OP_DLDATA, struct.pack('>I', block), (R_DLDATAACK,),
                retries=DATA_RETRIES, drain=False)
            if number != block:
                raise DncError(f'expected block {block}, device sent {number}')
            out += body
            if progress:
                progress(block, total)
        self.close_file()
        return bytes(out[:size])

    # ── convenience ──────────────────────────────────────────────────────────
    def find(self, path: str) -> Optional[Entry]:
        """Directory entry for `path`, or None; needed to learn a file's size."""
        name = basename(path).upper()
        for entry in self.list_dir(parent_of(path)):
            if entry.name.upper() == name:
                return entry
        return None

    def download_path(self, path: str, progress: ProgressFn = None) -> bytes:
        entry = self.find(path)
        if entry is None:
            raise FatFsError(FR_NO_FILE, path)
        return self.download(path, entry.size, progress)


# ──────────────────────────────── CLI ───────────────────────────────────────
def _cli(argv: List[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    ip, verb, rest = argv[1], argv[2].lower(), argv[3:]

    def show_progress(done: int, total: int) -> None:
        print(f'\r  block {done}/{total}', end='', flush=True)

    try:
        with QSClient(ip) as c:
            if c.local_port != PORT:
                print(f'note: bound local UDP port {c.local_port}, '
                      f'not {PORT} (needs root)')
            if verb == 'info':
                print(c.device_info())
            elif verb == 'status':
                s = c.read_status()
                print(f'{s.mode_text} / {s.process_text}  load {s.load_percent}%  '
                      f'cycle {s.cycle_minutes} min  {s.current_file}')
            elif verb == 'ls':
                path = rest[0] if rest else ROOT
                for e in c.list_dir(path):
                    kind = '<DIR>' if e.is_dir else f'{e.size:>9}'
                    print(f'  {kind}  {e.name}')
            elif verb == 'get':
                data = c.download_path(rest[0], show_progress)
                print()
                with open(rest[1], 'wb') as fh:
                    fh.write(data)
                print(f'wrote {rest[1]} ({len(data)} bytes)')
            elif verb == 'put':
                with open(rest[0], 'rb') as fh:
                    data = fh.read()
                c.upload(rest[1], data, show_progress)
                print(f'\nsent {len(data)} bytes')
            elif verb == 'run':
                c.run_file(rest[0])
                print('DNC started')
            elif verb == 'stop':
                c.stop_dnc()
                print('DNC stopped')
            elif verb == 'mkdir':
                c.mkdir(rest[0])
            elif verb == 'rm':
                c.delete_file(rest[0])
            else:
                print(f'unknown command: {verb}')
                return 2
            for note in c.notices:
                print(f'  device: {note}')
    except DncError as exc:
        print(f'ERROR: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(_cli(sys.argv))
