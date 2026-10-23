"""The machine browser dialog, pointed at the fake device on loopback."""

import time

import pytest

import machines as mach
import qsdnc


def wait_for(qapp, cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def popups(monkeypatch):
    """
    Modal boxes would wait forever for a click that never comes; record them
    instead, so an unexpected error fails the test rather than hanging it.
    """
    import dncui
    seen = []
    for name in ('warning', 'critical', 'information'):
        monkeypatch.setattr(dncui.QMessageBox, name,
                            staticmethod(lambda _p, title, text, *a, **k:
                                         seen.append((title, text))))
    monkeypatch.setattr(dncui.QMessageBox, 'question',
                        staticmethod(lambda *a, **k: pytest.fail(f'question: {a[1:3]}')))
    return seen


@pytest.fixture
def browser(qapp, device, monkeypatch, popups):
    import dncui
    monkeypatch.setattr(qsdnc, 'PORT', device.port)
    monkeypatch.setattr(qsdnc, 'BIND_PORT', 0)
    dialogs = []

    def make():
        dlg = dncui.MachineBrowserDialog(mach.Machine('Fake', '127.0.0.1'))
        dlg.show()
        dialogs.append(dlg)
        return dlg
    yield make
    for dlg in dialogs:
        dlg.close()
    qapp.processEvents()


def test_connects_and_lists_root(qapp, device, browser, popups):
    device.add_dir('program')
    device.add_file('PART.NC', b'x' * 1000)
    dlg = browser()
    assert wait_for(qapp, lambda: dlg.table.rowCount() == 2 or popups), popups
    assert popups == []
    assert 'Connected to Fake' in dlg.conn_label.text()
    assert [dlg.table.item(r, 0).text() for r in range(2)] == ['program', 'PART.NC']


def test_status_line_follows_the_device(qapp, device, browser):
    device.status = (6, 50, 2, 12, 'PART.NC')
    dlg = browser()
    assert wait_for(qapp, lambda: 'DNC / Running' in dlg.status_label.text())


def test_silent_device_leaves_dialog_open_and_disconnected(qapp, device, browser):
    device.silent = True
    dlg = browser()
    assert wait_for(qapp, lambda: 'Not connected to Fake' in dlg.conn_label.text(),
                    timeout=15)
    assert dlg.isVisible() and dlg.btn_reconnect.isVisible()


# ── naming and replacing on upload ───────────────────────────────────────────
@pytest.fixture
def typed(monkeypatch):
    """
    Script QInputDialog.getText: each answer is the text typed, or None for
    Cancel.  `typed.prompts` records the default text each prompt offered.
    """
    import dncui

    class Typed(list):
        prompts = []

    answers = Typed()

    def get_text(_parent, _title, _label, text=''):
        answers.prompts.append(text)
        if not answers:
            pytest.fail(f'unexpected prompt offering {text!r}')
        reply = answers.pop(0)
        return (reply, True) if reply is not None else ('', False)
    monkeypatch.setattr(dncui.QInputDialog, 'getText', staticmethod(get_text))
    return answers


@pytest.fixture
def ready(qapp, device, browser):
    device.add_file('OLD.NC', b'old program')
    device.add_dir('jobs')
    dlg = browser()
    assert wait_for(qapp, lambda: dlg.table.rowCount() == 2)
    return dlg


def answer_questions(monkeypatch, reply):
    import dncui
    asked = []
    monkeypatch.setattr(dncui.QMessageBox, 'question',
                        staticmethod(lambda _p, title, text, *a, **k:
                                     asked.append(text) or reply))
    return asked


def test_upload_offers_a_cleaned_name(qapp, device, ready, typed):
    typed.append('BRACKET O10.NC')                  # accept what was offered
    ready._start_upload(b'G00\r\n', 'Bracket Ø10 – rev°2.nc')
    assert typed.prompts == ['BRACKET O10 - REVDEG2.NC']
    assert wait_for(qapp, lambda: device.file('BRACKET O10.NC') == b'G00\r\n')


def test_unstorable_typed_name_is_explained_and_fixed(qapp, device, ready,
                                                      typed, popups):
    typed.extend(['Part Ø1.nc', 'Part O1.nc'])
    ready._start_upload(b'G00\r\n', 'PROGRAM.NC')
    assert 'cannot store Ø' in popups[0][1]
    assert 'Suggested instead: Part O1.nc' in popups[0][1]
    assert typed.prompts[1] == 'Part O1.nc'         # the retry offers the fix
    assert wait_for(qapp, lambda: device.file('Part O1.nc') == b'G00\r\n')


def test_cancelling_the_name_uploads_nothing(qapp, device, ready, typed):
    typed.append(None)
    ready._start_upload(b'G00\r\n', 'PROGRAM.NC')
    qapp.processEvents()
    assert device.count(0x02) == 0


@pytest.mark.parametrize('reply, expected', [('Yes', b'new'), ('No', b'old program')])
def test_replacing_a_file_asks_first(qapp, device, ready, typed, monkeypatch,
                                     reply, expected):
    from PyQt6.QtWidgets import QMessageBox
    asked = answer_questions(monkeypatch, getattr(QMessageBox.StandardButton, reply))
    typed.append('old.nc')                          # same name, other case
    ready._start_upload(b'new', 'PROGRAM.NC')
    assert len(asked) == 1 and 'OLD.NC' in asked[0] and 'Replace it?' in asked[0]
    time.sleep(0.3)
    qapp.processEvents()
    assert device.file('OLD.NC') == expected


def test_uploading_over_a_folder_is_refused(qapp, device, ready, typed, popups):
    typed.append('JOBS')
    ready._start_upload(b'new', 'PROGRAM.NC')
    assert 'already a folder named "jobs"' in popups[0][1]
    assert device.count(0x02) == 0


def test_new_folder_and_rename_check_names_too(qapp, device, ready, typed, popups):
    typed.extend(['Jöbs', 'Jobs2'])
    ready._on_mkdir()
    assert 'cannot store ö' in popups[0][1]
    assert wait_for(qapp, lambda: device.file('Jobs2') is None
                    and device._key('Jobs2') is not None)


def test_busy_device_says_to_close_its_menu(qapp, device, ready, popups):
    device.busy = -1
    ready._list(qsdnc.ROOT)
    assert wait_for(qapp, lambda: popups, timeout=10)
    assert 'close it and try again' in popups[0][1]
    assert 'Connected to Fake' in ready.conn_label.text()   # refused, not gone


def test_closing_mid_connect_returns_promptly(qapp, device, browser):
    device.silent = True
    dlg = browser()
    qapp.processEvents()
    t = time.monotonic()
    dlg.close()
    assert time.monotonic() - t < 2
