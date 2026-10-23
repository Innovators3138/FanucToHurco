"""Ctrl+F: find_spans() matching, and the FindBar driven with real key presses."""

import pytest

import main as m

TEXT = ('N10 G01 X1. (T1 G1 comment)\nN30G1X0.Y1.\nG10 G17 g1 G1.0 G91.1\n'
        'M3 M03 M30 ; M3 here\nT1M6')


def found(query):
    spans, code = m.find_spans(TEXT, query)
    return [TEXT[a:b] for a, b in spans], code


@pytest.mark.parametrize('query, matches', [
    ('G1', ['G01', 'G1', 'g1', 'G1.0']),       # by value; not G10/G17/comment
    ('g01', ['G01', 'G1', 'g1', 'G1.0']),
    ('G91', []),                               # G91.1 is a different code
    ('G91.1', ['G91.1']),
    ('M3', ['M3', 'M03']),                     # not M30, not after the ;
    ('T1', ['T1']),                            # glued to M6, outside the comment
    ('X1', ['X1.']),
])
def test_code_queries_match_by_value_outside_comments(query, matches):
    assert found(query) == (matches, True)


@pytest.mark.parametrize('query, matches', [
    ('comment', ['comment']),
    ('G1X', ['G1X']),
    ('m3 m03', ['M3 M03']),
])
def test_other_queries_are_case_insensitive_text(query, matches):
    assert found(query) == (matches, False)


def test_empty_query_finds_nothing():
    assert m.find_spans(TEXT, '  ') == ([], False)


def test_unclosed_comment_runs_to_end_of_line():
    spans, _ = m.find_spans('(G1 never closed\nG1', 'G1')
    assert spans == [(17, 19)]


def test_non_ascii_does_not_shift_offsets():
    text = 'É(G1)\nG1'
    spans, _ = m.find_spans(text, 'G1')
    assert [text[a:b] for a, b in spans] == ['G1']


# ── the widget ───────────────────────────────────────────────────────────────
@pytest.fixture
def window(qapp, sample_text):
    w = m.MainWindow()
    w.show()
    w.input_edit.setPlainText(sample_text)
    w.btn_convert.click()
    qapp.processEvents()
    yield w
    w.close()


def test_find_bar_steps_wraps_and_closes(window, qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    ed, bar = window.output_edit, window.output_find
    ed.setFocus()
    QTest.keyClick(ed, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert bar.isVisible() and qapp.focusWidget() is bar.field

    QTest.keyClicks(bar.field, 'G1')
    n = len(m.find_spans(ed.toPlainText(), 'G1')[0])
    assert bar.count.text().startswith(f'1 of {n}')
    assert ed.textCursor().selectedText() == 'G01'
    assert len(ed.extraSelections()) == n

    QTest.keyClick(bar.field, Qt.Key.Key_Return)
    assert bar.count.text().startswith(f'2 of {n}')
    QTest.keyClick(bar.field, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(bar.field, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert bar.count.text().startswith(f'{n} of {n}')       # wrapped

    QTest.keyClick(bar.field, Qt.Key.Key_Escape)
    assert not bar.isVisible() and ed.extraSelections() == []


def test_find_bars_are_per_pane(window):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    window.input_edit.setFocus()
    QTest.keyClick(window.input_edit, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert window.input_find.isVisible() and not window.output_find.isVisible()


def test_no_match_is_shown(window):
    bar = window.output_find
    bar.open()
    bar.field.setText('G2')
    assert bar.count.text() == 'No matches'
