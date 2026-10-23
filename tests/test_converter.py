"""Fanuc -> BNC conversion.  check() is the oracle wherever it can be."""

import math
import re

import pytest

import main as m


def run(src: str):
    out, warns = m.convert(src)
    problems, stats = m.check(out)
    assert problems == [], problems
    return out, warns


def blocks(out: str):
    """Output blocks without their N numbers, between the % and E lines."""
    return [re.sub(r'^N\d+', '', ln) for ln in out.splitlines()[1:-1]]


def words(block: str):
    return {L: float(v) for L, v in m.WORD.findall(block)}


# ── the reference program ────────────────────────────────────────────────────
def test_sample_program_converts_cleanly(sample_text):
    out, _warns = run(sample_text)
    lines = out.splitlines()
    assert lines[0] == '%' and lines[-1] == 'E'
    assert blocks(out)[:4] == ['G00', 'G90', 'G70', 'M25']
    assert blocks(out)[-1] == 'M02'


def test_sample_program_numbering_runs_by_two(sample_text):
    out, _ = run(sample_text)
    numbers = [int(re.match(r'N(\d+)', ln).group(1)) for ln in out.splitlines()[1:-1]]
    assert numbers == list(range(2, 2 * len(numbers) + 1, 2))


def test_sample_program_has_no_unsupported_words(sample_text):
    out, warns = run(sample_text)
    for unwanted in ('G43', 'G49', 'G54', 'G94', 'G98', 'G28', 'H'):
        assert unwanted not in out
    assert 'G43 not supported; removed' in warns


# ── header, units, end ───────────────────────────────────────────────────────
def test_metric_program_gets_g71():
    out, _ = run('G21\nG0 X10 Y10\nM30')
    assert blocks(out)[:4] == ['G00', 'G90', 'G71', 'M25']


def test_missing_m30_appends_end_sequence_and_warns():
    out, warns = run('G0 X1 Y1')
    assert blocks(out)[-1] == 'M02'
    assert 'no M30/M02 in source; end sequence added' in warns


def test_end_sequence_turns_off_spindle_and_coolant():
    out, _ = run('S1000 M3\nM8\nG0 X1 Y1\nM30')
    tail = blocks(out)[-6:]
    assert tail.index('M09') < tail.index('M05') < tail.index('M02')


def test_nothing_after_m30_is_converted():
    out, _ = run('G0 X1 Y1\nM30\nG0 X5 Y5')
    assert 'X5.' not in out


def test_comments_program_number_and_percent_are_dropped():
    out, _ = run('%\nO1234 (PART)\n(T1 G1 X9)\nG0 X1 Y1 ; trailing\nM30\n%')
    assert 'X9' not in out and 'O1234' not in out
    assert 'G00X1.Y1.' in blocks(out)


# ── one G code per block, absolute only ──────────────────────────────────────
def test_every_block_has_at_most_one_g_code(sample_text):
    out, _ = run(sample_text + '\nG17 G90 G0 X1 Y1')
    for b in blocks(out):
        assert len(re.findall(r'G\d', b)) <= 1, b


def test_g91_moves_become_absolute():
    out, warns = run('G90 G0 X1 Y1 Z.1\nG91 G1 X1 Y-0.5 F10\nX1\nM30')
    assert 'G01X2.Y0.5F10.' in blocks(out)
    assert 'X3.' in blocks(out)
    assert 'G91 incremental moves converted to absolute' in warns


def test_modal_motion_is_written_once():
    out, _ = run('G1 X1 Y1 F10\nX2\nX3\nM30')
    b = blocks(out)
    assert b.count('G01X1.Y1.F10.') == 1 and 'X2.' in b and 'X3.' in b


def test_feed_is_written_only_when_it_changes():
    out, _ = run('G1 X1 F10\nX2 F10\nX3 F20\nM30')
    assert [b for b in blocks(out) if 'F' in b] == ['G01X1.F10.', 'X3.F20.']


# ── arcs ─────────────────────────────────────────────────────────────────────
def test_r_format_arc_becomes_absolute_ij():
    out, _ = run('G0 X0 Y0 Z0.1\nG2 X1 Y1 R1 F10\nM30')
    assert 'G02X1.Y1.I1.J0.F10.' in blocks(out)


def test_negative_r_takes_the_long_way_round():
    out, _ = run('G0 X0 Y0 Z0.1\nG2 X1 Y1 R-1 F10\nM30')
    arc = next(b for b in blocks(out) if b.startswith('G02'))
    assert (words(arc)['I'], words(arc)['J']) == (0.0, 1.0)


def test_incremental_ij_is_made_absolute():
    out, _ = run('G0 X1 Y0 Z0\nG3 X-1 Y0 I-1 J0 F10\nM30')
    assert 'G03X-1.Y0.I0.J0.F10.' in blocks(out)


def test_absolute_ij_input_is_kept():
    out, _ = run('G90.1\nG0 X1 Y0 Z0\nG3 X-1 Y0 I0 J0 F10\nM30')
    assert 'G03X-1.Y0.I0.J0.F10.' in blocks(out)


def test_arc_omitting_unchanged_axes_still_writes_x_y_i_j():
    out, _ = run('G0 X1 Y0 Z0\nG3 X-1 I-1 F10\nM30')
    arc = next(b for b in blocks(out) if b.startswith('G03'))
    assert set('XYIJ') <= set(words(arc))


def test_helix_gets_k_pitch_per_turn_and_is_split():
    out, _ = run('G0 X1 Y0 Z0\nG3 X1 Y0 Z-0.2 I-1 J0 F10\nM30')
    arcs = [b for b in blocks(out) if 'I' in b]
    assert len(arcs) == 360 // m.HELIX_MAX_DEG
    assert all(words(b)['K'] == -0.2 for b in arcs)
    assert [words(b)['Z'] for b in arcs] == [-0.05, -0.1, -0.15, -0.2]


def test_half_turn_helix_pitch_is_per_full_turn():
    """K is the Z per 360 deg, not the Z of the block: half a turn doubles it."""
    out, _ = run('G0 X1 Y0 Z0\nG3 X-1 Y0 Z-0.1 I-1 J0 F10\nM30')
    assert {words(b)['K'] for b in blocks(out) if 'I' in b} == {-0.2}


def test_flat_full_circle_is_one_block():
    out, _ = run('G0 X1 Y0 Z-0.1\nG2 X1 Y0 I-1 J0 F10\nM30')
    assert len([b for b in blocks(out) if 'I' in b]) == 1


def test_g18_arc_is_linearised_within_tolerance():
    out, warns = run('G0 X1 Y1 Z1\nG18 G2 X1 Z-1 I0 K-1 F5\nM30')
    assert 'G18 arc converted to straight moves' in warns
    pts = [words(b) for b in blocks(out) if 'Z' in b and 'X' in b][1:]
    for p in pts:      # every point on the radius-1 circle about Z0 X1
        assert math.hypot(p['X'] - 1, p['Z']) == pytest.approx(1, abs=1e-4)
    assert (pts[-1]['X'], pts[-1]['Z']) == (1, -1)
    assert min(p['X'] for p in pts) == pytest.approx(0, abs=1e-4)   # bulges to -X


# ── M25, home, unknown Z ─────────────────────────────────────────────────────
def test_g28_z_becomes_m25():
    out, _ = run('G0 Z1\nG28 G91 Z0\nM30')
    assert 'G28' not in out and blocks(out).count('M25') >= 2


def test_g28_xy_is_dropped_with_warning():
    _, warns = run('G28 G91 X0 Y0\nM30')
    assert 'XY home/machine move dropped (no Hurco equivalent)' in warns


def test_incremental_z_after_m25_is_warned():
    """Z is unknown after M25: an incremental Z there cannot be resolved."""
    _, warns = run('G28 G91 Z0\nG91 G1 Z-0.1 F5\nM30')
    assert 'incremental move from unknown Z (after M25) - check' in warns


def test_tool_change_is_wrapped_in_m25():
    out, _ = run('T3 M6\nM30')
    b = blocks(out)
    i = b.index('T3M06')
    assert b[i - 1] == 'M25' and b[i + 1] == 'M25'


# ── canned cycles ────────────────────────────────────────────────────────────
def test_g81_expands_to_plain_moves():
    out, _ = run('G0 X1 Y1 Z0.5\nG81 X1 Y1 Z-0.25 R0.1 F5\nX2\nG80\nM30')
    assert not re.search(r'G8\d', out)
    b = blocks(out)
    assert b.count('G01Z-0.25F5.') + b.count('G01Z-0.25') == 2


def test_g83_pecks_down_in_q_steps():
    out, _ = run('G0 X1 Y1 Z0.5\nG83 X1 Y1 Z-0.25 R0.1 Q0.1 F5\nG80\nM30')
    feeds = [words(b)['Z'] for b in blocks(out)
             if b.startswith('G01') and set(words(b)) <= {'G', 'Z', 'F'}]
    assert feeds == [0.0, -0.1, -0.2, -0.25]


def test_g98_returns_to_initial_z_g99_to_r():
    out98, _ = run('G0 X1 Y1 Z0.5\nG98 G81 X1 Y1 Z-0.2 R0.1 F5\nG80\nM30')
    out99, _ = run('G0 X1 Y1 Z0.5\nG99 G81 X1 Y1 Z-0.2 R0.1 F5\nG80\nM30')
    last = lambda out: [words(b)['Z'] for b in blocks(out) if b.startswith('G00Z') or b == 'Z0.5'][-1]
    assert last(out98) == 0.5
    assert last(out99) == 0.1


def test_unsupported_cycle_is_remapped():
    _, warns = run('G0 X1 Y1 Z0.5\nG73 X1 Y1 Z-0.2 R0.1 Q0.05 F5\nG80\nM30')
    assert 'G73 not supported; mapped to G83 - verify' in warns


def test_native_mode_writes_cycle_template(monkeypatch):
    monkeypatch.setattr(m, 'CYCLE_MODE', 'native')
    out, _ = run('G0 X1 Y1 Z0.5\nG83 X1 Y1 Z-0.25 R0.1 Q0.1 F5\nX2\nG80\nM30')
    b = blocks(out)
    assert 'G83X1.Y1.Z0.1Z-0.25Z0.1F5.' in b
    assert 'X2.Y1.' in b and 'G80' in b


def test_g84_is_native_even_when_expanding():
    out, warns = run('G0 X1 Y1 Z0.5\nS500 M3\nG84 X1 Y1 Z-0.3 R0.1 F10\nG80\nM30')
    assert 'G84X1.Y1.Z0.1Z-0.3F10.' in blocks(out)
    assert 'G84 tapping sent as a native cycle - verify format on the machine' in warns


# ── words and limits ─────────────────────────────────────────────────────────
@pytest.mark.parametrize('src, block', [
    ('G4 P500', 'G04P0.5'),          # P without a decimal is milliseconds
    ('G4 P1.5', 'G04P1.5'),          # with one it is seconds
    ('G4 X2', 'G04P2.'),
])
def test_dwell_is_written_in_seconds(src, block):
    out, _ = run(src + '\nM30')
    assert block in blocks(out)


@pytest.mark.parametrize('src, warning', [
    ('T30 M6', 'tool T30 is above the BX limit of 24'),
    ('S9000 M3', 'S9000 outside BX spindle range 60-4000'),
    ('G54', 'G54 not supported; removed'),
    ('M98', 'M98 not supported; removed'),
    ('G95', 'G95 feed-per-rev found: F values will be WRONG on the Hurco'),
    ('T2', 'tool pre-select (T without M06) dropped'),
    ('G41 D1 X1 Y1', 'D (comp offset) word removed'),
])
def test_out_of_range_or_unsupported_is_warned_not_raised(src, warning):
    _, warns = run(src + '\nM30')
    assert warning in warns


def test_warnings_carry_source_line_numbers():
    _, warns = run('G0 X1\nG54\nG0 X2\nG54\nM30')
    assert warns['G54 not supported; removed'] == [2, 4]


def test_numbers_keep_a_decimal_point():
    out, _ = run('G0 X5 Y-3 Z0\nM30')
    assert 'G00X5.Y-3.Z0.' in blocks(out)
