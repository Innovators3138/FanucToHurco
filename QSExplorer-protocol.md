# Micro DNC / QS Explorer 4.06 — wire protocol

Reverse-engineered from `QSExplorer-4.06.exe` (Inno Setup 5.6 installer → `app/QSExplorer.exe`,
a .NET 4.x WinForms assembly compiled from VB.NET). Protocol logic lives in three classes:

| Class | Role |
|---|---|
| `HP_TFTP` | UDP socket wrapper |
| `TFTPParser` | builds request packets, parses responses |
| `TFTP_*Task` (`HPExplorer` namespace) | one class per operation |

`HPTreeLib.dll` is an unrelated shell-tree UI control. `Device.dat` is a plain-text
`"<name> <ip>"` list of known devices, one per line. `config.ini` is app name/version.

---

## 1. Transport

* **UDP**, IPv4. **Port 69 on both ends** — the client binds local port 69 and sends to
  device port 69. This is *not* real TFTP: there is no ephemeral TID/port negotiation.
* No authentication, no encryption, no session token. The device rejects a second
  concurrent client with opcode `0x63`.
* One socket per *operation*, not per session. `SendCommand()` closes and re-opens the
  socket before every command packet; `SendData()` reuses the socket opened by the
  previous `SendCommand()`.
* **Receive timeout: 800 ms** (`Socket.ReceiveTimeout = 0x320`).
* Retry policy (per operation class):
  * command packets: resend up to **5** times on timeout (10 for WRQ/close in the copy task)
  * data packets: resend up to **10** times (`DATA_RESEND_MAX = 10`)
* Socket bind is retried up to 5 times if port 69 is busy.

### Implication for a web version

Browsers cannot open UDP sockets. A web UI needs a small local relay
(Node `dgram` / Python `socket`) exposing WebSocket or HTTP to the page. Binding
source port 69 needs root on Linux/macOS; try an ephemeral source port first —
the device most likely answers the source port, but the stock client never tests that.

---

## 2. Packet framing

Everything is TFTP-shaped but with a **4-byte** block number instead of TFTP's 2-byte one.

**Command / request packet**

```
+--------+--------+--------------------+------+
| 0x00   | opcode | payload (ASCII)    | 0x00 |
+--------+--------+--------------------+------+
```

Opcode is a 16-bit big-endian value whose high byte is always 0, so on the wire it is
`00 <op>`. Strings are raw bytes of the .NET chars (`Convert.ToByte(char)`) — effectively
**Latin-1/ASCII**, NUL-terminated. No "mode" field (no `octet`/`netascii` string).

**DATA packet (client → device, upload)**

```
00 03 | block (uint32 BE) | payload (≤512 bytes)
```

**Data request (client → device, download)**

```
00 38 | block (uint32 BE)
```

**ACK (client → device)**

```
00 04 | block (uint32 BE)
```

**Every response** from the device has the same 6-byte header:

```
00 <op> | number (uint32 BE) | body...
```

`number` is overloaded: a block number for data ACKs, or an **`FR_*` result code**
(FatFs-style, see §5) for filesystem operations.

Header size is 6 bytes (`HEADER_BYTES_NUMBER = 6`, `HEADER_LAST_INDEX = 5`).
Responses shorter than 5 bytes are treated as malformed.

---

## 3. Opcodes

### Client → device

| Op | Name | Payload |
|---:|---|---|
| `0x01` | RRQ | `path\0` — built but unused by the GUI |
| `0x02` | WRQ (start upload) | `path\0` |
| `0x03` | DATA | `u32 block` + ≤512 data bytes |
| `0x04` | ACK | `u32 block` |
| `0x08` | ReadDir (next entry) | `u16 index` + `path\0` |
| `0x0A` | CreateNewFolder | `path\0` |
| `0x0C` | DeleteFile | `path\0` |
| `0x0E` | DeleteFolder | `path\0` |
| `0x10` | Rename | `oldpath\0newpath\0` |
| `0x13` | GetDeviceInfo | `\0\0` (two zero bytes) |
| `0x15` | SendMessage | `text\0` |
| `0x17` | StartUpCopyFile | `path\0` — defined, never sent by the GUI |
| `0x19` | RunFile (start DNC) | `path\0` |
| `0x1B` | OpenDir | `path\0` |
| `0x1C` | StopDnc | `\0` (empty string) |
| `0x32` | ReadStatus | `\0\0` |
| `0x34` | CloseFile | `"0"\0` |
| `0x36` | DownloadRQ (start download) | `path\0` |
| `0x38` | DownloadData | `u32 block` |

### Device → client

| Op | Name | Body after the 6-byte header |
|---:|---|---|
| `0x03` | Data | payload bytes |
| `0x04` | DataAck | — (`number` = block just accepted) |
| `0x05` | Error | message string starts at **offset 4**, NUL-terminated (classic TFTP layout: 2-byte error code at 2–3) |
| `0x07` | OpenDirAck | — (`number` = `FR_*`) |
| `0x09` | ReadDirData | directory entry, see §4.2 |
| `0x0B` | CreateNewFolderACK | — |
| `0x0D` | DeleteFileACK | — |
| `0x0F` | DeleteFolderACK | — |
| `0x11` | RenameACK | — |
| `0x12` | WaitACK | "operation in progress" — client waits for a **second** response |
| `0x14` | GetDeviceInfoACK | info string from offset 6, NUL-terminated |
| `0x16` | SendDeviceMsgACK | — |
| `0x18` | StartUpCopyFileACK | if length ≠ 5: string from offset 6 (device-initiated) |
| `0x1A` | StartRunFileACK | — |
| `0x1B` | DeviceDNCMode | string from offset 6 — device has entered DNC mode |
| `0x1D` | DeviceDNCStopACK | — |
| `0x33` | ReadStatusAck | status record, see §4.5 |
| `0x35` | CloseFileAck | — |
| `0x37` | DownloadRQAck | — (`number` = block) |
| `0x39` | DownloadDataAck | file payload from offset 6 |
| `0x63` | AnotherDeviceConnecting | another PC already owns the device |

---

## 4. Operation flows

Paths use `\` as separator and no drive letter — e.g. `\PROGRAMS\PART1.NC`.
The root is the empty string. The `0:` you see in the GUI is display-only.

### 4.1 Browse a directory

```
→ 00 1B  "<path>" 00                 OpenDir
← 00 07  <FR code>                   OpenDirAck   (FR != 0 → error, stop)
repeat:
  → 00 08  <u16 0> "<path>" 00       ReadDir  (index field is always 0; the device
  ← 00 09  <FR> <entry...>             keeps the cursor)
until entry index == 0xFFFF
```

### 4.2 ReadDirData entry layout

```
off 0 : 00 09
off 2 : u32 FR code   (non-zero → no entry, error)
off 6 : name bytes ... 00
      : u16 entry index (BE)   0xFFFF = end of listing
      : u8  attributes
      : u32 file size (BE)
```

Attribute bits (FatFs `AM_*`): `0x01 RDO`, `0x02 HID`, `0x04 SYS`, `0x08 VOL`,
`0x0F LFN`, `0x10 DIR`, `0x20 ARC`. The client treats `attr & 0x10` as a folder.
`.` and `..` are skipped.

### 4.3 Upload (PC → device)

```
→ 00 02 "<destpath>" 00              WRQ
← 00 04 <block>                      DataAck (or 0x63 = busy)
loop block = 1..N:
  → 00 03 <u32 block> <512 bytes>    DATA   (last block short; may be 0 bytes)
  ← 00 04 <u32 block>                DataAck — must equal the block just sent
→ 00 34 "0" 00                       CloseFile
← 00 35                              CloseFileAck
```

* Block size is **512 bytes**, blocks numbered from 1.
* `totalBlocks = ceil(filesize / 512)`.
* Mismatch between the acked block and the sent block triggers a resend (max 10).
* A **cancel** is a DATA packet with block number 0 and no payload:
  `00 03 00 00 00 00` (`CANCEL_COPY_PACKET`).

### 4.4 Download (device → PC)

```
→ 00 36 "<srcpath>" 00               DownloadRQ
← 00 37 <block>                      DownloadRQAck
loop block = 1..N:
  → 00 38 <u32 block>                request block
  ← 00 39 <u32 block> <payload>      data
→ 00 34 "0" 00                       CloseFile
← 00 35                              CloseFileAck
```

The client knows `N` in advance from the file size it got from the directory listing
(`ceil(size / 512)`); the protocol carries no explicit EOF marker.

### 4.5 Status polling

The GUI sends `00 32 00 00` to every known device every **1000 ms** and drains the
receive socket every **100 ms** (async, listening on port 69).

`ReadStatusAck` body:

```
off 6  : u8  device screen/mode
off 7  : u8  load percent (0–100)
off 8  : u8  process status
off 9  : u16 cycle time, minutes (BE)
off 11 : "<ip>|<current file path>" 00
```

Device mode: `0 Explorer, 1 About, 2 Settings, 3 Simulation, 4 Edit, 5 Read, 6 DNC`
Process status: `0 Stop, 1 Waiting, 2 Running`

### 4.6 Mutating operations (mkdir / delete / rename)

```
→ 00 0A "<path>" 00                  (or 0x0C / 0x0E / 0x10)
← 00 12                              WaitACK — optional, means "still working"
← 00 0B <FR code>                    final ACK
```

If the first response is `0x12`, read one more datagram (with up to 5 timeout retries)
to get the real result.

Rename payload is two NUL-terminated strings back to back:
`00 10 "<oldpath>" 00 "<newpath>" 00`.

### 4.7 Device info / message / run / stop

```
→ 00 13 00 00        → ← 00 14 <n> "<info text>" 00
→ 00 15 "<msg>" 00   → ← 00 16
→ 00 19 "<path>" 00  → ← 00 1A          starts DNC feed of that file
→ 00 1C 00           → ← 00 1D          stop DNC
```

---

## 5. Result codes (`number` field, FatFs `FR_*`)

```
0  FR_OK              6  FR_INVALID_NAME    11 FR_INVALID_DRIVE
1  FR_DISK_ERR        7  FR_DENIED          12 FR_NOT_ENABLED
2  FR_INT_ERR         8  FR_EXIST           13 FR_NO_FILESYSTEM
3  FR_NOT_READY       9  FR_INVALID_OBJECT  14 FR_MKFS_ABORTED
4  FR_NO_FILE        10  FR_WRITE_PROTECTED 15 FR_TIMEOUT
5  FR_NO_PATH
```

The `FR_*` naming plus the `AM_*` attribute bits mean the device runs **FatFs** on an
SD card, with this protocol as a thin RPC over its `f_opendir` / `f_readdir` / `f_open` /
`f_unlink` / `f_rename` calls.

---

## 6. Gotchas for a re-implementation

1. **Block numbers are 32-bit**, unlike standard TFTP. A stock TFTP library will not work.
2. **Both endpoints use port 69.** Standard TFTP servers switch to an ephemeral port after
   the first packet; this one does not.
3. Only *one* client at a time — `0x63` means another PC holds the device.
4. Strings are byte-per-char, so non-ASCII filenames will corrupt. Stick to ASCII.
5. `0x12 WaitACK` can precede any filesystem ACK; always be ready for two datagrams.
6. There is no length or checksum field. Trust the UDP checksum and the 800 ms timeout.
7. The device pushes unsolicited `0x1B` (entered DNC mode) and `0x18` packets — a robust
   client should tolerate them arriving mid-exchange.
