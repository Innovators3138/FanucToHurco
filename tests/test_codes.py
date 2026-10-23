"""The codes-used panel: counting, labels, and click-to-find."""

import pytest

import main as m


def test_counts_merge_spellings_and_skip_comments():
    text = 'G1 X1 (G1 T9)\nN30G01X2.\nG1.0 G10 M3 M03 ; M3\nT2M6'
    assert m.code_counts(text) == {('G', 1.0): 3, ('G', 10.0): 1, ('M', 3.0): 2,
                                   ('T', 2.0): 1, ('M', 6.0): 1}


def test_counts_only_the_letters_asked_for():
    assert set(m.code_counts('G0 X1 Y2 M3 S500 T1')) == {('G', 0), ('M', 3), ('T', 1)}


@pytest.mark.parametrize('key, label', [
    (('G', 0.0), 'G00'), (('G', 83.0), 'G83'), (('M', 3.0), 'M03'),
    (('T', 1.0), 'T1'), (('T', 12.0), 'T12'), (('G', 91.1), 'G91.1'),
])
def test_labels(key, label):
    assert m.code_label(*key) == label


def test_every_label_finds_its_own_code():
    text = 'G0 G00 G91.1 M3 T1 T12'
    for key, n in m.code_counts(text).items():
        assert len(m.find_spans(text, m.code_label(*key))[0]) == n


def test_every_code_the_bx_mpu_takes_is_described():
    for g in m.ALLOWED_G:
        assert ('G', g) in m.BX_CODES, f'G{g}'
    for code in m.ALLOWED_M:
        assert ('M', code) in m.BX_CODES, f'M{code}'


def test_every_fanuc_code_the_converter_handles_is_described():
    handled = ({20, 21, 28, 30, 53, 43, 49, 54, 94, 95, 98, 99, 90.1, 91.1,
                10, 65, 18, 19} | set(m.CYCLE_MAP))
    for g in handled:
        assert ('G', g) in m.BX_CODES or ('G', g) in m.FANUC_CODES, f'G{g}'
    assert ('M', 30) in m.FANUC_CODES


@pytest.mark.parametrize('key, text', [
    (('G', 81), 'Drill cycle'),
    (('M', 25), 'Z retract to machine home'),
    (('G', 43), 'Tool length compensation + (Fanuc only; the BX-MPU does not use it)'),
    (('G', 70), 'Hurco BX: English (inch) units\nFanuc: Lathe finishing cycle; not used on a mill'),
    (('G', 91.1), 'Arc centers I/J incremental (Fanuc only; the BX-MPU does not use it)'),
    (('T', 3), 'Tool 3: the tool the next M06 loads'),
    (('G', 12), 'Not a code the BX-MPU or a standard Fanuc mill defines'),
])
def test_descriptions(key, text):
    assert m.code_description(*key) == text


# ── the widget ───────────────────────────────────────────────────────────────
@pytest.fixture
def window(qapp, sample_text):
    w = m.MainWindow()
    w.show()
    w.input_edit.setPlainText(sample_text)
    w.btn_convert.click()
    w.codes.refresh()            # skip the debounce
    yield w
    w.close()


def rows(panel):
    t = panel.table
    return {t.item(r, 0).text(): (t.item(r, 1).text(), t.item(r, 2).text())
            for r in range(t.rowCount())}


def row_of(panel, label):
    return next(r for r in range(panel.table.rowCount())
                if panel.table.item(r, 0).text() == label)


def test_panel_shows_what_the_converter_changed(window):
    got = rows(window.codes)
    assert got['G00'] == ('4', '18')
    assert got['G81'] == ('1', '')           # drilling expanded into moves
    assert got['M25'] == ('', '6')           # retracts the converter wrote
    assert list(got)[:2] == ['G00', 'G01'] and list(got)[-1] == 'T1'   # G, M, T


def test_dropped_and_added_codes_are_coloured(window):
    t, panel = window.codes.table, window.codes
    assert t.item(row_of(panel, 'G81'), 0).foreground().color() == panel.DROPPED
    assert t.item(row_of(panel, 'M25'), 0).foreground().color() == panel.ADDED
    assert t.item(row_of(panel, 'G00'), 0).foreground().color() != panel.ADDED


def test_code_cell_describes_the_code_count_cells_keep_their_tip(window):
    t, panel = window.codes.table, window.codes
    r = row_of(panel, 'G81')
    assert t.item(r, 0).toolTip() == 'G81: Drill cycle'
    assert t.item(r, 1).toolTip() == 'In the input only: removed or replaced'
    assert t.item(row_of(panel, 'M25'), 2).toolTip() == \
        'In the output only: added by the converter'
    assert t.item(row_of(panel, 'G00'), 1).toolTip() == ''     # in both


def test_clicking_a_count_finds_and_steps(window):
    panel, bar = window.codes, window.output_find
    r = row_of(panel, 'M25')
    panel._on_click(r, 2)
    assert bar.isVisible() and bar.field.text() == 'M25'
    assert bar.count.text().startswith('1 of 6')
    assert window.output_edit.textCursor().selectedText() == 'M25'
    panel._on_click(r, 2)
    assert bar.count.text().startswith('2 of 6')


def test_clicking_a_code_goes_where_it_is(window):
    panel = window.codes
    panel._on_click(row_of(panel, 'G81'), 0)       # input only
    assert window.input_find.isVisible() and not window.output_find.isVisible()
    panel._on_click(row_of(panel, 'G00'), 0)       # in both: the output
    assert window.output_find.field.text() == 'G00'


def test_clicking_an_empty_count_does_nothing(window):
    panel = window.codes
    panel._on_click(row_of(panel, 'M25'), 1)
    assert not window.input_find.isVisible()


def test_panel_follows_edits(window, qapp):
    from PyQt6.QtTest import QTest
    window.input_edit.setPlainText('G0 X1\nM8\nM30')
    QTest.qWait(500)                               # past the debounce
    assert rows(window.codes)['M08'][0] == '1'
    assert 'G81' not in rows(window.codes)
