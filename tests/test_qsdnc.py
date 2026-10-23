"""The DNC protocol client, against the loopback fake device."""

import math
import time

import pytest

import qsdnc
from qsdnc import DncBusy, DncRemoteError, DncTimeout, DncAborted, FatFsError


def gcode(size: int) -> bytes:
    line = b'N10G01X1.2345Y-2.5F25.\r\n'
    return (line * (size // len(line) + 1))[:size]


# ── info, status, control ────────────────────────────────────────────────────
def test_device_info(client):
    assert client.device_info() == 'MICRO DNC2'


def test_read_status(client, device):
    device.status = (6, 42, 2, 300, r'0:\program\PART.NC')
    s = client.read_status()
    assert (s.mode_text, s.load_percent, s.process_text, s.cycle_minutes) == \
           ('DNC', 42, 'Running', 300)
    assert s.ip == '127.0.0.1'
    assert s.current_file == r'0:\program\PART.NC'


def test_run_stop_and_message(client, device):
    client.run_file('\\program\\PART.NC')
    client.stop_dnc()
    client.send_message('hello')
    assert device.ran == ['\\program\\PART.NC']
    assert device.stopped == 1
    assert device.messages == ['hello']


# ── listing ──────────────────────────────────────────────────────────────────
def test_list_root_puts_folders_first_then_names(client, device):
    device.add_file('b.nc', b'12345')
    device.add_dir('program')
    device.add_file('A.NC', b'')
    entries = client.list_dir()
    assert [(e.name, e.is_dir, e.size) for e in entries] == \
           [('program', True, 0), ('A.NC', False, 0), ('b.nc', False, 5)]


def test_list_subfolder_skips_dot_entries(client, device):
    device.add_dir('program')
    device.add_file('program\\3D.NC', b'x' * 259373)
    entries = client.list_dir('program')
    assert [(e.name, e.size) for e in entries] == [('3D.NC', 259373)]


def test_list_walks_the_index_past_255(client, device):
    """ReadDir is index-addressed; a u16 index has to survive past one byte."""
    for i in range(300):
        device.add_file(f'F{i:03d}.NC', b'')
    names = [e.name for e in client.list_dir()]
    assert names == [f'F{i:03d}.NC' for i in range(300)]
    sent = [int.from_bytes(p[0:2], 'big') for op, p in device.received if op == 0x08]
    assert sent == list(range(301))          # 300 entries + the end marker


def test_list_missing_folder_reports_no_path(client):
    with pytest.raises(FatFsError) as exc:
        client.list_dir('nowhere')
    assert exc.value.code == 5


def test_find_ignores_case(client, device):
    device.add_dir('program')
    device.add_file('program\\Part.nc', b'abc')
    assert client.find('\\PROGRAM\\PART.NC').size == 3
    assert client.find('\\program\\other.nc') is None


# ── transfers ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('size', [0, 1, 511, 512, 513, 1024, 1025, 5000])
def test_upload_then_download_round_trips(client, device, size):
    data = gcode(size)
    blocks = client.upload('T.NC', data)
    assert blocks == math.ceil(size / 512)
    assert device.file('T.NC') == data
    assert client.download_path('T.NC') == data


def test_every_byte_value_survives(client, device):
    data = bytes(range(256)) * 4
    client.upload('BIN.NC', data)
    assert client.download_path('BIN.NC') == data


def test_overwrite_with_shorter_file_leaves_no_tail(client, device):
    client.upload('T.NC', gcode(2000))
    client.upload('T.NC', gcode(700))
    assert client.download_path('T.NC') == gcode(700)


def test_upload_progress_counts_every_block(client):
    seen = []
    client.upload('T.NC', gcode(1300), lambda d, t: seen.append((d, t)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_download_missing_file_reports_no_file(client):
    with pytest.raises(FatFsError) as exc:
        client.download_path('NOPE.NC')
    assert exc.value.code == 4


def test_lost_data_block_is_resent_and_not_doubled(client, device):
    device.drop[0x03] = 1                     # first DATA packet vanishes
    data = gcode(1500)
    client.upload('T.NC', data)
    assert device.file('T.NC') == data
    assert device.count(0x03) == 4           # 3 blocks + 1 resend


def test_lost_download_block_is_requested_again(client, device):
    device.add_file('T.NC', gcode(1500))
    device.drop[0x38] = 1
    assert client.download_path('T.NC') == gcode(1500)


# ── mutating operations ──────────────────────────────────────────────────────
def test_mkdir_rename_delete(client, device):
    client.mkdir('jobs')
    client.upload('jobs\\A.NC', b'abc')
    client.rename('jobs\\A.NC', 'jobs\\B.NC')
    assert [e.name for e in client.list_dir('jobs')] == ['B.NC']
    assert client.download_path('jobs\\B.NC') == b'abc'
    client.delete_file('jobs\\B.NC')
    client.delete_folder('jobs')
    assert client.list_dir() == []


@pytest.mark.parametrize('call, code', [
    (lambda c: c.mkdir('jobs'), 8),                 # already exists
    (lambda c: c.delete_file('jobs\\NOPE.NC'), 4),  # no such file
    (lambda c: c.delete_folder('jobs'), 7),         # not empty
    (lambda c: c.rename('NOPE.NC', 'X.NC'), 4),
])
def test_filesystem_errors_carry_the_fr_code(client, device, call, code):
    device.add_dir('jobs')
    device.add_file('jobs\\A.NC', b'')
    with pytest.raises(FatFsError) as exc:
        call(client)
    assert exc.value.code == code


def test_wait_ack_is_waited_out_without_resending(client, device):
    """Resending a delete after WaitACK could run it twice."""
    device.add_file('A.NC', b'')
    device.wait_ack = {0x0C}
    device.wait_delay = 0.5                   # longer than the 0.2 s timeout
    client.delete_file('A.NC')
    assert device.count(0x0C) == 1
    assert device.file('A.NC') is None


# ── busy, loss, pushes, refusals ─────────────────────────────────────────────
def test_brief_busy_is_resent_quickly_and_not_reported(device):
    # The full 0.8 s timeout: waiting it out twice would take 1.6 s.
    c = qsdnc.QSClient('127.0.0.1', port=device.port, bind_port=0)
    with c:
        device.busy = 2
        t = time.monotonic()
        assert c.list_dir() == []
        assert time.monotonic() - t < 0.6
        assert device.count(0x1B) == 3
        assert c.notices == []


def test_endless_busy_gives_up(client, device, monkeypatch):
    monkeypatch.setattr(qsdnc, 'BUSY_WAIT', 0.3)
    device.busy = -1
    with pytest.raises(DncTimeout, match='stayed busy'):
        client.list_dir()


def test_busy_does_not_block_info_or_status(client, device):
    device.busy = -1                          # the device's own menu is open
    assert client.device_info() == 'MICRO DNC2'
    client.read_status()


def test_lost_reply_is_retried(client, device):
    device.drop[0x1B] = 2
    assert client.list_dir() == []
    assert device.count(0x1B) == 3


def test_unsolicited_pushes_are_recorded_not_mistaken(client, device):
    device.push = [bytes((0, 0x1B)) + bytes(4) + b'PART.NC\x00',
                   bytes((0, 0x18)) + bytes(4) + b'COPY.NC\x00']
    assert client.device_info() == 'MICRO DNC2'
    assert client.notices == ['entered DNC mode: PART.NC', 'startup copy: COPY.NC']


def test_another_pc_holding_the_device_raises_busy(client, device):
    device.owned = True
    with pytest.raises(DncBusy):
        client.device_info()


def test_error_packet_raises_remote_error(client, device):
    device.error[0x19] = (2, 'file locked')
    with pytest.raises(DncRemoteError, match='device error 2: file locked'):
        client.run_file('A.NC')


def test_silent_device_times_out(client, device):
    device.silent = True
    t = time.monotonic()
    with pytest.raises(DncTimeout):
        client.device_info()
    # 1 try + CMD_RETRIES retries at 0.2 s each
    assert time.monotonic() - t == pytest.approx(0.2 * (qsdnc.CMD_RETRIES + 1), abs=0.3)


def test_should_stop_aborts_between_retries(device):
    device.silent = True
    stop = []
    c = qsdnc.QSClient('127.0.0.1', port=device.port, bind_port=0, timeout=0.2,
                       should_stop=lambda: bool(stop))
    with c:
        stop.append(True)
        t = time.monotonic()
        with pytest.raises(DncAborted):
            c.device_info()
        assert time.monotonic() - t < 0.1


# ── path helpers ─────────────────────────────────────────────────────────────
def test_path_helpers():
    assert qsdnc.join(qsdnc.ROOT, 'A.NC') == '\\A.NC'
    assert qsdnc.join('\\program\\', 'A.NC') == '\\program\\A.NC'
    assert qsdnc.parent_of('\\program\\A.NC') == '\\program'
    assert qsdnc.parent_of('\\A.NC') == qsdnc.ROOT
    assert qsdnc.basename('\\program\\A.NC') == 'A.NC'
