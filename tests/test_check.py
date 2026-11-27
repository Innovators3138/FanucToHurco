"""check() must catch broken output, not just pass good output."""

import pytest

import main as m

GOOD = '\n'.join(['%', 'N2G00', 'N4G90', 'N6G70', 'N8M25', 'N10G00X1.Y0.Z0.',
                  'N12G03X0.Y1.Z-0.05I0.J0.K-0.2F10.', 'N14M02', 'E']) + '\n'


def problems(text):
    return m.check(text)[0]


def test_good_output_passes():
    assert problems(GOOD) == []


@pytest.mark.parametrize('find, replace, expected', [
    ('%\n', '', 'first line is not %'),
    ('\nE\n', '\n', 'last line is not E'),
    ('N4G90', 'N6G90', 'N sequence break'),
    ('N4G90', 'N4G90G00', 'more than one G code'),
    ('N4G90', 'N4G43', 'unsupported G43'),
    ('N14M02', 'N14M98', 'unsupported M98'),
    ('N4G90', 'N4G91.1', 'G90.x/G91.x'),
    ('N4G90', 'N4G90Q1.', 'unexpected word Q'),
    ('N4G90', 'N4 G90', 'bad block'),
    ('I0.J0.K', 'I0.K', 'arc missing J'),
    ('X0.Y1.Z-0.05I0.', 'X0.Y1.1Z-0.05I0.', 'arc radius error'),
    ('K-0.2', '', 'helical arc without K'),
    ('K-0.2', 'K-0.3', 'K pitch disagrees with Z travel'),
    # rules the BX firmware enforces
    ('N4G90', 'N4G90P1.', 'unexpected word P'),
    ('N14M02', 'N14M05M02', 'more than one M code'),
    ('N4G90', 'N4G04', 'BAD DWELL TIME'),
    ('N4G90', 'N4G04/10000', 'BAD DWELL TIME'),
    ('N4G90', 'N4G90/5', '/dwell word without G04'),
    ('N4G90', 'N4G41D1', 'D without T'),
    ('N4G90', 'N4T2S900', "S in a T block"),
    ('N4G90', 'N4M06', 'M06 without T'),
    ('F10.', 'F260.', 'BAD FEED RATE'),
    ('N4G90', 'N4G80', 'G80 DOES NOT FOLLOW'),
    ('N10G00X1.Y0.Z0.', 'N10G00X1.Y0.Z0.I1.', 'IJK & NON-CIRCULAR MOTION'),
    ('N10G00X1.Y0.Z0.', 'N10G00X1.Y0.Z0.Z1.', 'EXTRA Z COMMAND'),
])
def test_broken_output_is_reported(find, replace, expected):
    broken = GOOD.replace(find, replace, 1)
    assert broken != GOOD
    assert any(expected in p for p in problems(broken)), problems(broken)


def test_arc_right_after_m25_is_from_unknown_z_not_an_error():
    text = GOOD.replace('N10G00X1.Y0.Z0.', 'N10G00X1.Y0.')
    assert problems(text) == []


def test_stats():
    _, stats = m.check(GOOD)
    assert stats == {'arcs': 1, 'worst_radius_error': 0.0,
                     'characters': len(GOOD), 'blocks': 7}


CYCLE = '\n'.join(['%', 'N2G00', 'N4G90', 'N6G70', 'N8M25', 'N10G00X1.Y1.Z0.1',
                   'N12G83X1.Y1.Z0.5Z0.2Z0.1F5.', 'N14X2.Y1.', 'N16G80',
                   'N18M02', 'E']) + '\n'


def test_good_native_cycle_passes():
    assert problems(CYCLE) == []


@pytest.mark.parametrize('find, replace, expected', [
    ('Z0.5Z0.2Z0.1F5.', 'Z0.5Z0.2Z0.1', 'NO FEED RATE'),
    ('Z0.5Z0.2Z0.1', 'Z0.5Z0.6Z0.1', 'BAD Z IN CANNED CYCLE'),
    ('G83X1.Y1.Z0.5Z0.2Z0.1', 'G83X1.Y1.Z0.5', 'G83 needs 2-3 Z words'),
    ('G83X1.Y1.Z0.5Z0.2Z0.1', 'G81X1.Y1.Z0.5Z0.2', 'G81 needs 1 Z words'),
    ('G83X1.Y1.Z0.5Z0.2Z0.1', 'G83X1.Y1.', 'MISSING Z IN CANNED CYCLE'),
    ('G83X1.Y1.Z0.5Z0.2Z0.1', 'G82X1.Y1.Z0.5', 'G82 without a G04 dwell'),
    ('N10G00X1.Y1.Z0.1', 'N10G00X1.Y1.', 'CANNED Z FROM UNKNOWN POSITION'),
    ('N14X2.Y1.', 'N14X2.Y1.Z0.3', 'Z word on a canned-cycle hole block'),
    ('N16G80', 'N16G01X3.', 'while canned cycle G83 is active'),
])
def test_broken_native_cycle_is_reported(find, replace, expected):
    broken = CYCLE.replace(find, replace, 1)
    assert broken != CYCLE
    assert any(expected in p for p in problems(broken)), problems(broken)


def test_g04_may_share_a_block_with_g82():
    text = CYCLE.replace('G83X1.Y1.Z0.5Z0.2Z0.1', 'G82G04/5X1.Y1.Z0.5')
    assert problems(text) == []


def test_numbering_may_start_again_after_n_max(monkeypatch):
    monkeypatch.setattr(m, 'N_MAX', 8)
    text = GOOD.replace('N10', 'N2').replace('N12', 'N4').replace('N14', 'N6')
    assert problems(text) == []
