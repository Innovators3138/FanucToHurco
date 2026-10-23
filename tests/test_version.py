"""Which build this is: version.py and the window title."""

import subprocess

import version


def test_a_stamped_build_reports_its_stamp(monkeypatch):
    monkeypatch.setattr(version, 'VERSION', 'v1.0')
    assert version.app_version() == 'v1.0'


def test_a_source_run_is_dev(monkeypatch):
    monkeypatch.setattr(version, 'VERSION', 'dev')
    assert version.app_version().startswith('dev')


def test_a_source_run_without_git_is_plain_dev(monkeypatch):
    def no_git(*_a, **_k):
        raise FileNotFoundError('git')
    monkeypatch.setattr(version, 'VERSION', 'dev')
    monkeypatch.setattr(subprocess, 'run', no_git)
    assert version.app_version() == 'dev'


def test_the_stamp_the_workflows_append_wins(tmp_path):
    """The workflows append a second VERSION line; the later one must count."""
    src = open(version.__file__, encoding='utf-8').read() + "VERSION = 'v9.9'\n"
    namespace = {'__file__': str(tmp_path / 'version.py')}
    exec(compile(src, 'version.py', 'exec'), namespace)
    assert namespace['app_version']() == 'v9.9'


def test_window_title_shows_the_version(qapp, monkeypatch):
    import main as m
    monkeypatch.setattr(m, 'app_version', lambda: 'v1.0')
    w = m.MainWindow()
    assert w.windowTitle().startswith('FanucToHurco v1.0  —')
    w.close()
