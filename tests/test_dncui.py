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


def test_device_error_is_reported_not_fatal(qapp, device, browser, popups):
    device.add_file('PART.NC', b'')
    dlg = browser()
    assert wait_for(qapp, lambda: dlg.table.rowCount() == 1)
    device.busy = -1                    # its own file menu is open, say
    dlg._list(qsdnc.ROOT)
    assert wait_for(qapp, lambda: popups, timeout=10)
    assert 'busy' in popups[0][1]
    assert 'Connected to Fake' in dlg.conn_label.text()


def test_closing_mid_connect_returns_promptly(qapp, device, browser):
    device.silent = True
    dlg = browser()
    qapp.processEvents()
    t = time.monotonic()
    dlg.close()
    assert time.monotonic() - t < 2
