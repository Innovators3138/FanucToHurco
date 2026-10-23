"""
A Micro DNC stand-in on loopback UDP, behaving the way the real box was
observed to (QSExplorer-protocol.md), with knobs for the awkward parts:

    busy       reply "Micro DNC is being busy." to the next N filesystem
               commands (-1: until reset)
    wait_ack   opcodes whose final ACK is preceded by 0x12 WaitACK and held
               back for `wait_delay` seconds
    drop       {opcode: n} — ignore the next n packets with that opcode
    push       raw packets to send ahead of the next reply (unsolicited)
    error      {opcode: (code, text)} — answer that opcode with a 0x05 Error
    owned      answer everything with 0x63 (another PC holds the device)
    silent     answer nothing

`received` records every packet as (opcode, payload) in arrival order.
"""

import socket
import threading
from typing import Dict, List, Optional, Tuple

BUSY_TEXT = b'Micro DNC is being busy.'
FS_OPS = {0x02, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x1B, 0x36}
BLOCK = 512


def _resp(op: int, number: int = 0, body: bytes = b'') -> bytes:
    return bytes((0, op)) + number.to_bytes(4, 'big') + body


def _norm(path: str) -> str:
    """Device paths may or may not lead with a backslash; store them without."""
    return path.strip('\\')


def _strings(payload: bytes) -> List[str]:
    return [s.decode('latin-1') for s in payload.split(b'\x00')]


class FakeDevice:
    def __init__(self, info: str = 'MICRO DNC2') -> None:
        self.info = info
        # Full path -> bytes for a file, None for a folder.  Lookups ignore
        # case, as FatFs does; names keep the case they were created with.
        self.fs: Dict[str, Optional[bytes]] = {}
        self.busy = 0
        self.wait_ack: set = set()
        self.wait_delay = 0.0
        self.drop: Dict[int, int] = {}
        self.push: List[bytes] = []
        self.error: Dict[int, Tuple[int, str]] = {}
        self.owned = False
        self.silent = False
        self.received: List[Tuple[int, bytes]] = []
        self.ran: List[str] = []
        self.messages: List[str] = []
        self.stopped = 0
        self.status = (0, 0, 0, 0, 'No File Selected')

        self._upload: Optional[Tuple[str, bytearray, int]] = None
        self._download: Optional[bytes] = None
        self._lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # ── setup helpers ────────────────────────────────────────────────────────
    def add_dir(self, path: str) -> None:
        self.fs[_norm(path)] = None

    def add_file(self, path: str, data: bytes) -> None:
        self.fs[_norm(path)] = data

    def file(self, path: str) -> Optional[bytes]:
        key = self._key(path)
        return None if key is None else self.fs[key]

    def count(self, op: int) -> int:
        return sum(1 for o, _p in self.received if o == op)

    def close(self) -> None:
        self.sock.close()
        self._thread.join(timeout=2)

    # ── filesystem ───────────────────────────────────────────────────────────
    def _key(self, path: str) -> Optional[str]:
        low = _norm(path).lower()
        return next((k for k in self.fs if k.lower() == low), None)

    def _is_dir(self, path: str) -> bool:
        path = _norm(path)
        return path == '' or (self._key(path) is not None
                              and self.fs[self._key(path)] is None)

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit('\\', 1)[0] if '\\' in path else ''

    def _children(self, path: str) -> List[str]:
        low = _norm(path).lower()
        return [k for k in self.fs if self._parent(k).lower() == low]

    def _entries(self, path: str) -> List[Tuple[str, int, int]]:
        """(name, attr, size) in the order the device hands them out."""
        out = [] if _norm(path) == '' else [('.', 0x10, 0), ('..', 0x10, 0)]
        for k in self._children(path):
            data = self.fs[k]
            name = k.rsplit('\\', 1)[-1]
            out.append((name, 0x10, 0) if data is None
                       else (name, 0x20, len(data)))
        return out

    # ── protocol ─────────────────────────────────────────────────────────────
    def _serve(self) -> None:
        while True:
            try:
                raw, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            if len(raw) < 2:
                continue
            op, payload = raw[1], raw[2:]
            with self._lock:
                self.received.append((op, payload))
                if self.silent:
                    continue
                if self.drop.get(op, 0) > 0:
                    self.drop[op] -= 1
                    continue
                replies = self._handle(op, payload)
            for pkt in self.push:
                self.sock.sendto(pkt, addr)
            self.push = []
            if op in self.wait_ack and replies:
                self.sock.sendto(_resp(0x12), addr)
                timer = threading.Timer(self.wait_delay, self._send_all,
                                        (replies, addr))
                timer.daemon = True
                timer.start()
            else:
                self._send_all(replies, addr)

    def _send_all(self, replies: List[bytes], addr) -> None:
        for pkt in replies:
            try:
                self.sock.sendto(pkt, addr)
            except OSError:
                return

    def _handle(self, op: int, payload: bytes) -> List[bytes]:
        if self.owned:
            return [_resp(0x63)]
        if op in self.error:
            code, text = self.error[op]
            return [bytes((0, 0x05)) + code.to_bytes(2, 'big')
                    + text.encode('latin-1') + b'\x00']
        if op in FS_OPS and self.busy:
            if self.busy > 0:
                self.busy -= 1
            return [_resp(0x1B, 0, BUSY_TEXT + b'\x00')]
        handler = getattr(self, f'_op_{op:02x}', None)
        return handler(payload) if handler else []

    def _op_13(self, _p):                                   # GetDeviceInfo
        return [_resp(0x14, 0, self.info.encode('latin-1') + b'\x00')]

    def _op_32(self, _p):                                   # ReadStatus
        mode, load, proc, cycle, cur = self.status
        body = (bytes((mode, load, proc)) + cycle.to_bytes(2, 'big')
                + f'127.0.0.1|{cur}'.encode('latin-1') + b'\x00')
        return [_resp(0x33, 0, body)]

    def _op_15(self, p):                                    # SendMessage
        self.messages.append(_strings(p)[0])
        return [_resp(0x16)]

    def _op_19(self, p):                                    # RunFile
        self.ran.append(_strings(p)[0])
        return [_resp(0x1A)]

    def _op_1c(self, _p):                                   # StopDnc
        self.stopped += 1
        return [_resp(0x1D)]

    def _op_1b(self, p):                                    # OpenDir
        return [_resp(0x07, 0 if self._is_dir(_strings(p)[0]) else 5)]

    def _op_08(self, p):                                    # ReadDir
        index = int.from_bytes(p[0:2], 'big')
        path = _strings(p[2:])[0]
        entries = self._entries(path)
        if index >= len(entries):
            return [_resp(0x09, 0, b'\x00\xff\xff' + bytes(5))]
        name, attr, size = entries[index]
        body = (name.encode('latin-1') + b'\x00' + (index + 1).to_bytes(2, 'big')
                + bytes((attr,)) + size.to_bytes(4, 'big'))
        return [_resp(0x09, 0, body)]

    def _op_0a(self, p):                                    # CreateNewFolder
        path = _norm(_strings(p)[0])
        if self._key(path) is not None:
            return [_resp(0x0B, 8)]
        if not self._is_dir(self._parent(path)):
            return [_resp(0x0B, 5)]
        self.fs[path] = None
        return [_resp(0x0B, 0)]

    def _op_0c(self, p):                                    # DeleteFile
        key = self._key(_strings(p)[0])
        if key is None:
            return [_resp(0x0D, 4)]
        if self.fs[key] is None:
            return [_resp(0x0D, 7)]
        del self.fs[key]
        return [_resp(0x0D, 0)]

    def _op_0e(self, p):                                    # DeleteFolder
        key = self._key(_strings(p)[0])
        if key is None:
            return [_resp(0x0F, 4)]
        if self.fs[key] is not None or self._children(key):
            return [_resp(0x0F, 7)]
        del self.fs[key]
        return [_resp(0x0F, 0)]

    def _op_10(self, p):                                    # Rename
        old, new = _strings(p)[:2]
        key = self._key(old)
        if key is None:
            return [_resp(0x11, 4)]
        if self._key(new) is not None:
            return [_resp(0x11, 8)]
        self.fs[_norm(new)] = self.fs.pop(key)
        return [_resp(0x11, 0)]

    def _op_02(self, p):                                    # WRQ
        path = _norm(_strings(p)[0])
        self._upload = (path, bytearray(), 0)
        return [_resp(0x04, 0)]

    def _op_03(self, p):                                    # DATA
        block = int.from_bytes(p[0:4], 'big')
        if block == 0:                                      # cancel
            self._upload = None
            return []
        if self._upload is None:
            return []
        path, buf, last = self._upload
        if block == last + 1:
            buf += p[4:]
            self._upload = (path, buf, block)
        # A repeat of the last block (our ACK was lost) is re-acked, not re-added.
        return [_resp(0x04, block)]

    def _op_34(self, _p):                                   # CloseFile
        if self._upload is not None:
            path, buf, _last = self._upload
            key = self._key(path) or path
            self.fs[key] = bytes(buf)
            self._upload = None
        self._download = None
        return [_resp(0x35)]

    def _op_36(self, p):                                    # DownloadRQ
        key = self._key(_strings(p)[0])
        self._download = self.fs.get(key) if key else None
        return [_resp(0x37, 0 if self._download is not None else 4)]

    def _op_38(self, p):                                    # DownloadData
        block = int.from_bytes(p[0:4], 'big')
        data = self._download or b''
        return [_resp(0x39, block, data[(block - 1) * BLOCK: block * BLOCK])]
