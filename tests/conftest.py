import os
import sys

# Before anything imports Qt: no display needed, here or in CI.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest                      # noqa: E402

import qsdnc                       # noqa: E402
from fake_device import FakeDevice  # noqa: E402

SAMPLE = os.path.join(ROOT, 'program_out.nc')


@pytest.fixture
def device():
    dev = FakeDevice()
    yield dev
    dev.close()


@pytest.fixture
def client(device):
    """A client on loopback with a short timeout, so failures fail fast."""
    c = qsdnc.QSClient('127.0.0.1', port=device.port, bind_port=0, timeout=0.2)
    c.open()
    yield c
    c.close()


@pytest.fixture(scope='session')
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope='session')
def sample_text():
    with open(SAMPLE, encoding='utf-8') as fh:
        return fh.read()
