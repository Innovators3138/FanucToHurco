#!/usr/bin/env python3
"""
dncui.py  —  Qt front end for the Micro DNC / QS Explorer protocol.

  ClientThread          owns the UDP socket; every device call runs here
  MachineEditDialog     name + IP form
  MachineManagerDialog  the saved machine list
  MachineBrowserDialog  remote file browser, transfers and DNC control

The device allows one PC at a time and every call can block for seconds of
timeouts and retries, so no protocol call is ever made on the GUI thread.
"""

import os
import queue
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import machines as mach
import qsdnc


# ─────────────────────────────── worker thread ──────────────────────────────
class ClientThread(QThread):
    """
    Serialises every device call onto one thread that owns one QSClient.

    Work is submitted as `(tag, fn)` where `fn(client)` returns a result; the
    tag comes back with the result so the dialog knows what finished.
    """

    connected = pyqtSignal(str, int)        # device info, local UDP port
    result    = pyqtSignal(str, object)     # tag, value
    failed    = pyqtSignal(str, str)        # tag, message
    progress  = pyqtSignal(str, int, int)   # tag, blocks done, blocks total
    notice    = pyqtSignal(str)             # unsolicited device push

    def __init__(self, ip: str, parent=None) -> None:
        super().__init__(parent)
        self._ip = ip
        self._queue: 'queue.Queue[Optional[Tuple[str, Callable]]]' = queue.Queue()
        self._stopping = False

    def submit(self, tag: str, fn: Callable[['qsdnc.QSClient'], object]) -> None:
        self._queue.put((tag, fn))

    def shutdown(self) -> bool:
        """
        Ask the worker to stop and wait for it; False if it is still going.

        The flag matters as much as the sentinel: a call that is unanswered
        burns seconds of retries, so anything already queued behind it has to
        be abandoned rather than run on the way out.
        """
        self._stopping = True
        self._queue.put(None)
        return self.wait(5000)

    def run(self) -> None:
        # Read the ports through the module so a test (or an odd install) can
        # point the client somewhere other than 69.
        client = qsdnc.QSClient(self._ip, port=qsdnc.PORT,
                                bind_port=qsdnc.BIND_PORT,
                                should_stop=lambda: self._stopping)
        try:
            client.open()
            info = client.device_info()
        except qsdnc.DncAborted:
            client.close()          # closed before it ever answered; say nothing
            return
        except qsdnc.DncError as exc:
            self.failed.emit('connect', str(exc))
            client.close()
            return
        except OSError as exc:
            self.failed.emit('connect', f'network error: {exc}')
            client.close()
            return

        self.connected.emit(info, client.local_port or 0)
        try:
            while True:
                item = self._queue.get()
                if item is None or self._stopping:
                    break
                tag, fn = item
                try:
                    value = fn(client)
                except qsdnc.DncAborted:
                    break           # shutting down; the dialog is not listening
                except qsdnc.DncError as exc:
                    self.failed.emit(tag, str(exc))
                    continue
                except OSError as exc:
                    self.failed.emit(tag, f'network error: {exc}')
                    continue
                except Exception as exc:                  # never kill the thread
                    self.failed.emit(tag, f'{type(exc).__name__}: {exc}')
                    continue
                finally:
                    for note in client.notices:
                        self.notice.emit(note)
                    client.notices.clear()
                self.result.emit(tag, value)
        finally:
            client.close()


# ───────────────────────────── machine list UI ──────────────────────────────
class MachineEditDialog(QDialog):
    """Add or edit one machine."""

    def __init__(self, parent=None, machine: Optional[mach.Machine] = None,
                 existing: Optional[List[mach.Machine]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Edit Machine' if machine else 'Add Machine')
        self.setMinimumWidth(360)
        self._editing = machine
        self._existing = existing or []

        self.name_edit = QLineEdit(machine.name if machine else '')
        self.name_edit.setPlaceholderText('e.g. Hurco MB-1')
        self.ip_edit = QLineEdit(machine.ip if machine else '')
        self.ip_edit.setPlaceholderText('e.g. 192.168.1.50')

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('Machine name'))
        lay.addWidget(self.name_edit)
        lay.addWidget(QLabel('IP address'))
        lay.addWidget(self.ip_edit)
        self.error = QLabel('')
        self.error.setStyleSheet('color: #ff8080; font-weight: normal;')
        self.error.setWordWrap(True)
        lay.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _on_ok(self) -> None:
        problem = mach.validate(self.name_edit.text(), self.ip_edit.text(),
                                self._existing, self._editing)
        if problem:
            self.error.setText(problem)
            return
        self.accept()

    def machine(self) -> mach.Machine:
        return mach.Machine(self.name_edit.text().strip(),
                            self.ip_edit.text().strip())


class MachineManagerDialog(QDialog):
    """The saved machine list.  `machines()` returns the edited list."""

    def __init__(self, parent=None, machines: Optional[List[mach.Machine]] = None):
        super().__init__(parent)
        self.setWindowTitle('Manage Machines')
        self.resize(460, 320)
        self._machines: List[mach.Machine] = list(machines or [])

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._on_edit())

        btn_add    = QPushButton('Add…')
        btn_edit   = QPushButton('Edit…')
        btn_remove = QPushButton('Remove')
        btn_import = QPushButton('Import Device.dat…')
        btn_add.clicked.connect(self._on_add)
        btn_edit.clicked.connect(self._on_edit)
        btn_remove.clicked.connect(self._on_remove)
        btn_import.clicked.connect(self._on_import)

        side = QVBoxLayout()
        for b in (btn_add, btn_edit, btn_remove, btn_import):
            side.addWidget(b)
        side.addStretch()

        row = QHBoxLayout()
        row.addWidget(self.list, stretch=1)
        row.addLayout(side)

        lay = QVBoxLayout(self)
        lay.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._refresh()

    def machines(self) -> List[mach.Machine]:
        return self._machines

    def _refresh(self, select: int = -1) -> None:
        self.list.clear()
        for m in self._machines:
            self.list.addItem(QListWidgetItem(m.label()))
        if 0 <= select < len(self._machines):
            self.list.setCurrentRow(select)

    def _selected(self) -> int:
        return self.list.currentRow()

    def _on_add(self) -> None:
        dlg = MachineEditDialog(self, existing=self._machines)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._machines.append(dlg.machine())
            self._refresh(len(self._machines) - 1)

    def _on_edit(self) -> None:
        row = self._selected()
        if row < 0:
            return
        dlg = MachineEditDialog(self, self._machines[row], self._machines)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._machines[row] = dlg.machine()
            self._refresh(row)

    def _on_remove(self) -> None:
        row = self._selected()
        if row < 0:
            return
        target = self._machines[row]
        if QMessageBox.question(self, 'Remove Machine',
                                f'Remove {target.label()} from the list?'
                                ) != QMessageBox.StandardButton.Yes:
            return
        del self._machines[row]
        self._refresh(min(row, len(self._machines) - 1))

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Import QS Explorer Device.dat', '',
            'Device list (Device.dat *.dat);;All files (*)')
        if not path:
            return
        try:
            found = mach.import_device_dat(path)
        except OSError as exc:
            QMessageBox.warning(self, 'Import failed', str(exc))
            return
        added = 0
        for m in found:
            if mach.validate(m.name, m.ip, self._machines) is None:
                self._machines.append(m)
                added += 1
        self._refresh(len(self._machines) - 1)
        QMessageBox.information(
            self, 'Import Device.dat',
            f'Added {added} machine(s).' +
            (f'  Skipped {len(found) - added} already in the list.'
             if len(found) > added else ''))


def choose_machine(parent, machines: List[mach.Machine]) -> Optional[mach.Machine]:
    """Pick one machine; returns immediately when there is only one."""
    if not machines:
        QMessageBox.information(
            parent, 'No Machines',
            'No machines are configured yet.\n\n'
            'Use Machines ▸ Manage Machines… to add one.')
        return None
    if len(machines) == 1:
        return machines[0]
    labels = [m.label() for m in machines]
    label, ok = QInputDialog.getItem(parent, 'Select Machine', 'Machine:',
                                     labels, 0, False)
    if not ok:
        return None
    return machines[labels.index(label)]


# ──────────────────────────── remote file browser ───────────────────────────
def _fmt_size(n: int) -> str:
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{n / 1024:.1f} KB'
    return f'{n / (1024 * 1024):.1f} MB'


class MachineBrowserDialog(QDialog):
    """
    Browse one machine's card, transfer files and start or stop the DNC feed.

    `pending_upload` queues a transfer that is offered as soon as the listing
    arrives — that is how "Send Output to Machine" hands over the converted
    program without a second dialog.

    The dialog opens whether or not the device answers: the status line at the
    top carries the connection state and Reconnect retries it in place.
    """

    # Consecutive failed status polls before the device is called gone.  The
    # poll runs once a second and each failure has already burnt its retries,
    # so this is seconds of silence, not a blip.
    _STATUS_FAIL_LIMIT = 3

    def __init__(self, machine: mach.Machine, parent=None,
                 pending_upload: Optional[Tuple[bytes, str]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f'{machine.name} — {machine.ip}')
        self.resize(720, 520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._machine = machine
        self._path = qsdnc.ROOT
        self._entries: List[qsdnc.Entry] = []
        self._busy = False
        self._ctx: Dict[str, object] = {}
        self._pending_upload = pending_upload
        self._thread: Optional[ClientThread] = None
        self._connected = False
        self._status_failures = 0
        self._status_pending = False
        self._closing = False

        self._build_ui()

        self._poll = QTimer(self)
        self._poll.setInterval(1000)            # the stock client's poll rate
        self._poll.timeout.connect(self._tick_status)

        self._start_thread()

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)

        conn = QHBoxLayout()
        self.conn_label = QLabel('')
        self.conn_label.setWordWrap(True)
        self.btn_reconnect = QPushButton('Reconnect')
        self.btn_reconnect.setVisible(False)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        conn.addWidget(self.conn_label, stretch=1)
        conn.addWidget(self.btn_reconnect)
        lay.addLayout(conn)

        bar = QHBoxLayout()
        self.btn_up      = QPushButton('↑ Up')
        self.btn_refresh = QPushButton('Refresh')
        self.btn_upload  = QPushButton('Upload…')
        self.btn_download= QPushButton('Download…')
        self.btn_mkdir   = QPushButton('New Folder')
        self.btn_rename  = QPushButton('Rename')
        self.btn_delete  = QPushButton('Delete')
        for b in (self.btn_up, self.btn_refresh, self.btn_upload,
                  self.btn_download, self.btn_mkdir, self.btn_rename,
                  self.btn_delete):
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        self.path_label = QLabel('0:\\')
        lay.addWidget(self.path_label)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Name', 'Size', 'Type'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.table, stretch=1)

        dnc = QHBoxLayout()
        self.btn_run  = QPushButton('▶ Start DNC on Selected')
        self.btn_stop = QPushButton('■ Stop DNC')
        self.btn_msg  = QPushButton('Send Message…')
        for b in (self.btn_run, self.btn_stop, self.btn_msg):
            dnc.addWidget(b)
        dnc.addStretch()
        lay.addLayout(dnc)

        self.status_label = QLabel('—')
        lay.addWidget(self.status_label)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.msg_label = QLabel('Connecting…')
        self.msg_label.setWordWrap(True)
        lay.addWidget(self.msg_label)

        self.btn_up.clicked.connect(self._on_up)
        self.btn_refresh.clicked.connect(lambda: self._list(self._path))
        self.btn_upload.clicked.connect(self._on_upload)
        self.btn_download.clicked.connect(self._on_download)
        self.btn_mkdir.clicked.connect(self._on_mkdir)
        self.btn_rename.clicked.connect(self._on_rename)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_msg.clicked.connect(self._on_message)

    # ── small helpers ────────────────────────────────────────────────────────
    def _say(self, text: str) -> None:
        self.msg_label.setText(text)

    def _set_busy(self, busy: bool, text: str = '') -> None:
        self._busy = busy
        # Nothing here works without a device, so a disconnected dialog stays
        # greyed out however the busy flag moves.
        enabled = (not busy) and self._connected
        for b in (self.btn_up, self.btn_refresh, self.btn_upload,
                  self.btn_download, self.btn_mkdir, self.btn_rename,
                  self.btn_delete, self.btn_run, self.btn_stop, self.btn_msg):
            b.setEnabled(enabled)
        self.table.setEnabled(enabled)
        if text:
            self._say(text)
        if not busy:
            self.bar.setVisible(False)

    def _selected_entry(self) -> Optional[qsdnc.Entry]:
        row = self.table.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def _display_path(self) -> str:
        return '0:' + (self._path or '\\')

    # ── connection state ─────────────────────────────────────────────────────
    _CONN_STYLE = {
        'connecting':   ('#ddaa44', 'Connecting to {name} at {ip}…'),
        'connected':    ('#44cc88', 'Connected to {name} at {ip}'),
        'disconnected': ('#ff8080', 'Not connected to {name}'),
    }

    def _set_connection(self, state: str, detail: str = '') -> None:
        """Drive the status line at the top: connecting / connected / not."""
        color, template = self._CONN_STYLE[state]
        text = template.format(name=self._machine.name, ip=self._machine.ip)
        if detail:
            text += f'  —  {detail}'
        self.conn_label.setText(text)
        self.conn_label.setStyleSheet(f'color: {color}; font-weight: bold;')
        self._connected = (state == 'connected')
        self.btn_reconnect.setVisible(state == 'disconnected')
        self.btn_reconnect.setEnabled(state == 'disconnected')

    def _start_thread(self) -> None:
        """Spin up a worker and attempt the connection."""
        self._status_failures = 0
        self._status_pending = False
        self._set_connection('connecting')
        thread = ClientThread(self._machine.ip, self)
        thread.connected.connect(self._on_connected)
        thread.result.connect(self._on_result)
        thread.failed.connect(self._on_failed)
        thread.progress.connect(self._on_progress)
        thread.notice.connect(lambda t: self._say(f'Device: {t}'))
        self._thread = thread
        thread.start()
        self._set_busy(True, f'Connecting to {self._machine.ip}…')

    def _stop_thread(self) -> None:
        """Release the socket and stop any signal from a dying worker."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        # Explicitly, signal by signal: a wildcard disconnect() also tears at
        # QThread's own connections and Qt warns about it.
        for sig in (thread.connected, thread.result, thread.failed,
                    thread.progress, thread.notice):
            try:
                sig.disconnect()         # nothing lands after this point
            except TypeError:
                pass                     # nothing was connected to it
        if thread.isRunning() and not thread.shutdown():
            # Still inside a call's retries.  Destroying a running QThread
            # aborts the process, so cut it loose and let it delete itself
            # once it lands and releases the socket.
            thread.setParent(None)
            thread.finished.connect(thread.deleteLater)
            return
        thread.deleteLater()

    def _drop_connection(self, detail: str) -> None:
        """Connection is gone: say so in red and leave the dialog open."""
        self._poll.stop()
        self._stop_thread()
        self._set_connection('disconnected', detail)
        self._set_busy(False)
        self.status_label.setText('—')

    def _on_reconnect(self) -> None:
        self.btn_reconnect.setEnabled(False)
        self._stop_thread()              # a QThread cannot be restarted
        self._start_thread()

    def _link(self) -> Optional[ClientThread]:
        """The worker to talk to, or None (said so) when the link is down."""
        if self._thread is None or not self._connected:
            self._set_busy(False)
            self._say(f'Not connected to {self._machine.name} — press '
                      'Reconnect first.')
            return None
        return self._thread

    def _submit(self, tag: str, fn: Callable[['qsdnc.QSClient'], object]) -> None:
        link = self._link()
        if link is not None:
            link.submit(tag, fn)

    def queue_upload(self, data: bytes, suggested: str) -> None:
        """Hand a program to a browser that is already open for this machine."""
        self.raise_()
        self.activateWindow()
        if self._connected and not self._busy:
            self._start_upload(data, suggested)
            return
        self._pending_upload = (data, suggested)
        self._say(f'{suggested} is queued — it will be offered as soon as '
                  f'{self._machine.name} is ready.')

    # ── device calls ─────────────────────────────────────────────────────────
    def _list(self, path: str) -> None:
        if self._busy or not self._connected or self._thread is None:
            return
        self._ctx = {'path': path}
        self._set_busy(True, f'Listing {path or chr(92)}…')
        self._submit('list', lambda c: c.list_dir(path))

    def _tick_status(self) -> None:
        if self._busy or not self._connected or self._thread is None:
            return                              # never queue behind a transfer
        if self._status_pending:
            # A silent device takes seconds of retries to fail, far longer than
            # the one-second tick; queueing another would only deepen the hole.
            return
        self._status_pending = True
        self._submit('status', lambda c: c.read_status())

    # ── slots: connection ────────────────────────────────────────────────────
    def _on_connected(self, info: str, local_port: int) -> None:
        if self._closing:
            return
        self._set_connection('connected', info)
        note = f'Connected — {info}' if info else 'Connected'
        if local_port and local_port != qsdnc.PORT:
            note += (f'   [local UDP port {local_port}; port {qsdnc.PORT} '
                     'needs root — if the device does not answer, run as '
                     'administrator]')
        self._set_busy(False, note)   # clear the "Connecting…" hold first
        self._poll.start()
        self._list(qsdnc.ROOT)

    def _on_failed(self, tag: str, message: str) -> None:
        if self._closing:
            return
        if tag == 'connect':
            # No pop-up and no auto-close: the red status line says it, and the
            # dialog stays up so Reconnect is one click away.
            self._drop_connection(message)
            hint = ('  The program is still queued and will be offered once '
                    'the machine answers.' if self._pending_upload else '')
            self._say('Check that the machine is switched on, that the Micro '
                      f'DNC box is on the network, and that {self._machine.ip} '
                      'is its address.' + hint)
            return
        if tag == 'status':
            # The poll is the only thing that keeps running on its own, so a
            # run of failures is what tells us the device went away.
            self._status_pending = False
            self._status_failures += 1
            if self._status_failures >= self._STATUS_FAIL_LIMIT and not self._busy:
                self._drop_connection(f'connection lost — {message}')
                self._say(f'Lost contact with {self._machine.label()}: {message}')
                return
            self.status_label.setText(f'status unavailable — {message}')
            return
        self._set_busy(False)
        self._say(f'{tag}: {message}')
        QMessageBox.warning(self, 'Device error', message)

    def _on_progress(self, tag: str, done: int, total: int) -> None:
        if self._closing:
            return
        self.bar.setVisible(True)
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)
        self._say(f'{tag.capitalize()}: block {done} of {total}')

    # ── slots: results ───────────────────────────────────────────────────────
    def _on_result(self, tag: str, value: object) -> None:
        if self._closing:
            return
        handler = getattr(self, f'_done_{tag}', None)
        if handler is not None:
            handler(value)

    def _done_status(self, status) -> None:
        self._status_failures = 0
        self._status_pending = False
        self.status_label.setText(
            f'{status.mode_text} / {status.process_text}   '
            f'load {status.load_percent}%   cycle {status.cycle_minutes} min'
            + (f'   {status.current_file}' if status.current_file else ''))

    def _done_list(self, entries) -> None:
        self._path = str(self._ctx.get('path', qsdnc.ROOT))
        self._entries = list(entries)
        self.path_label.setText(self._display_path())
        self.table.setRowCount(len(self._entries))
        for row, e in enumerate(self._entries):
            self.table.setItem(row, 0, QTableWidgetItem(e.name))
            self.table.setItem(row, 1, QTableWidgetItem(
                '' if e.is_dir else _fmt_size(e.size)))
            self.table.setItem(row, 2, QTableWidgetItem(
                'Folder' if e.is_dir else 'File'))
        self.btn_up.setEnabled(bool(self._path))
        self._set_busy(False, f'{len(self._entries)} item(s) in '
                              f'{self._display_path()}')
        self.btn_up.setEnabled(bool(self._path))
        if self._pending_upload is not None:
            data, name = self._pending_upload
            self._pending_upload = None
            self._start_upload(data, name)

    def _done_upload(self, _value) -> None:
        self._set_busy(False, 'Upload complete.')
        self._list(self._path)

    def _done_download(self, data: bytes) -> None:
        target = str(self._ctx.get('local', ''))
        try:
            with open(target, 'wb') as fh:
                fh.write(data)
        except OSError as exc:
            self._set_busy(False)
            QMessageBox.warning(self, 'Could not save', str(exc))
            return
        self._set_busy(False, f'Saved {len(data)} bytes to {target}')

    def _done_mkdir(self, _v) -> None:
        self._set_busy(False, 'Folder created.')
        self._list(self._path)

    def _done_delete(self, _v) -> None:
        self._set_busy(False, 'Deleted.')
        self._list(self._path)

    def _done_rename(self, _v) -> None:
        self._set_busy(False, 'Renamed.')
        self._list(self._path)

    def _done_run(self, _v) -> None:
        self._set_busy(False, 'DNC started — the machine can now read the '
                              'program.')

    def _done_stop(self, _v) -> None:
        self._set_busy(False, 'DNC stopped.')

    def _done_message(self, _v) -> None:
        self._set_busy(False, 'Message sent.')

    # ── slots: navigation ────────────────────────────────────────────────────
    def _on_double_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._entries) and self._entries[row].is_dir:
            self._list(qsdnc.join(self._path, self._entries[row].name))

    def _on_up(self) -> None:
        if self._path:
            self._list(qsdnc.parent_of(self._path))

    # ── slots: transfers ─────────────────────────────────────────────────────
    def _on_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Upload to machine', '',
            'NC programs (*.nc *.bnc *.txt *.gcode);;All files (*)')
        if not path:
            return
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as exc:
            QMessageBox.warning(self, 'Could not read file', str(exc))
            return
        self._start_upload(data, os.path.basename(path))

    def _start_upload(self, data: bytes, suggested: str) -> None:
        name, ok = QInputDialog.getText(
            self, 'Upload', f'Save on the machine as (in {self._display_path()}):',
            text=suggested.upper())
        if not ok or not name.strip():
            return
        remote = qsdnc.join(self._path, name.strip())
        tag = 'upload'
        link = self._link()
        if link is None:
            return
        emit = link.progress.emit
        self._set_busy(True, f'Uploading {len(data)} bytes to {remote}…')
        link.submit(tag, lambda c: c.upload(remote, data,
                                            lambda d, t: emit(tag, d, t)))

    def _on_download(self) -> None:
        entry = self._selected_entry()
        if entry is None or entry.is_dir:
            QMessageBox.information(self, 'Download', 'Select a file first.')
            return
        local, _ = QFileDialog.getSaveFileName(
            self, 'Save downloaded file as', entry.name,
            'NC programs (*.nc *.bnc *.txt);;All files (*)')
        if not local:
            return
        remote = qsdnc.join(self._path, entry.name)
        size = entry.size
        tag = 'download'
        link = self._link()
        if link is None:
            return
        emit = link.progress.emit
        self._ctx = {'local': local}
        self._set_busy(True, f'Downloading {remote}…')
        link.submit(tag, lambda c: c.download(remote, size,
                                              lambda d, t: emit(tag, d, t)))

    # ── slots: filesystem ────────────────────────────────────────────────────
    def _on_mkdir(self) -> None:
        name, ok = QInputDialog.getText(self, 'New Folder', 'Folder name:')
        if not ok or not name.strip():
            return
        target = qsdnc.join(self._path, name.strip())
        self._set_busy(True, f'Creating {target}…')
        self._submit('mkdir', lambda c: c.mkdir(target))

    def _on_rename(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        name, ok = QInputDialog.getText(self, 'Rename', 'New name:',
                                        text=entry.name)
        if not ok or not name.strip() or name.strip() == entry.name:
            return
        old = qsdnc.join(self._path, entry.name)
        new = qsdnc.join(self._path, name.strip())
        self._set_busy(True, f'Renaming {entry.name}…')
        self._submit('rename', lambda c: c.rename(old, new))

    def _on_delete(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        kind = 'folder' if entry.is_dir else 'file'
        if QMessageBox.question(
                self, 'Delete',
                f'Delete the {kind} "{entry.name}" from {self._machine.name}?\n'
                'This cannot be undone.') != QMessageBox.StandardButton.Yes:
            return
        target = qsdnc.join(self._path, entry.name)
        is_dir = entry.is_dir
        self._set_busy(True, f'Deleting {entry.name}…')
        self._submit(
            'delete',
            lambda c: c.delete_folder(target) if is_dir else c.delete_file(target))

    # ── slots: DNC ───────────────────────────────────────────────────────────
    def _on_run(self) -> None:
        entry = self._selected_entry()
        if entry is None or entry.is_dir:
            QMessageBox.information(self, 'Start DNC', 'Select a program first.')
            return
        target = qsdnc.join(self._path, entry.name)
        if QMessageBox.question(
                self, 'Start DNC',
                f'Start drip-feeding "{entry.name}" on {self._machine.name}?\n\n'
                'Make sure the machine is ready to receive.'
                ) != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True, f'Starting DNC on {target}…')
        self._submit('run', lambda c: c.run_file(target))

    def _on_stop(self) -> None:
        self._set_busy(True, 'Stopping DNC…')
        self._submit('stop', lambda c: c.stop_dnc())

    def _on_message(self) -> None:
        text, ok = QInputDialog.getText(self, 'Send Message',
                                        'Message to show on the device:')
        if not ok or not text.strip():
            return
        self._set_busy(True, 'Sending message…')
        self._submit('message', lambda c: c.send_message(text.strip()))

    # ── teardown ─────────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:
        # Set first: a connect attempt can still be inside its retries, and a
        # slot that runs after the dialog is on its way out would touch widgets
        # that are about to be deleted.
        self._closing = True
        self._poll.stop()
        self._stop_thread()
        super().closeEvent(event)
