"""The saved machine list."""

import pytest

import machines as mach
from machines import Machine


@pytest.mark.parametrize('name, ip, message', [
    ('', '10.0.0.1', 'Give the machine a name.'),
    ('Mill', '', 'Give the machine an IP address.'),
    ('Mill', '10.0.0', 'is not a valid IPv4 address'),
    ('mill ', '10.0.0.9', 'There is already a machine named'),
    ('Lathe', '10.0.0.1', 'already uses 10.0.0.1'),
])
def test_validate_rejects(name, ip, message):
    existing = [Machine('Mill', '10.0.0.1')]
    assert message in mach.validate(name, ip, existing)


def test_validate_allows_editing_a_machine_in_place():
    mill = Machine('Mill', '10.0.0.1')
    assert mach.validate('Mill', '10.0.0.1', [mill], editing=mill) is None


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / 'sub' / 'machines.json')
    ms = [Machine('Mill', '192.168.30.10'), Machine('Lathe', '192.168.30.11')]
    mach.save(ms, path)
    assert mach.load(path) == ms


@pytest.mark.parametrize('content', ['', 'not json', '{"a": 1}', '[{"name": "x"}]'])
def test_damaged_file_loads_as_empty(tmp_path, content):
    path = tmp_path / 'machines.json'
    path.write_text(content)
    assert mach.load(str(path)) == []


def test_missing_file_loads_as_empty(tmp_path):
    assert mach.load(str(tmp_path / 'none.json')) == []


def test_import_device_dat(tmp_path):
    dat = tmp_path / 'Device.dat'
    dat.write_text('# comment\nKMB-1 Mill 192.168.30.10\n\nbad 999.1.1.1\n'
                   'Lathe 192.168.30.11\n')
    assert mach.import_device_dat(str(dat)) == [
        Machine('KMB-1 Mill', '192.168.30.10'), Machine('Lathe', '192.168.30.11')]
