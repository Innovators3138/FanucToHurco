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
