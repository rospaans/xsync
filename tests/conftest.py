"""Fixtures shared by the tests."""

import subprocess
from pathlib import Path

import pytest

from xsync import app as app_module
from xsync import project as project_module
from xsync.output import Printer, state
from helpers import GNU_RSYNC, FakeScreen, Sandbox, make_project_folder


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Every test starts and ends with xsync's shared state as it is at startup, and never asks GitHub for updates."""
    monkeypatch.setenv("XSYNC_NO_UPDATE_CHECK", "1")
    saved = (state.pretty, state.changes, dict(state.last_counts), state.force_colors)
    state.output = Printer()
    yield
    state.output = Printer()
    state.pretty, state.changes, state.force_colors = saved[0], saved[1], saved[3]
    state.last_counts.update(saved[2])


@pytest.fixture
def project(tmp_path):
    """A source folder and an empty destination, for the rsync functions."""
    return Sandbox(tmp_path)


@pytest.fixture
def hub(tmp_path):
    """An app with one project (alpha → a local folder, a few files) and a fake terminal."""
    tmp_path = tmp_path.resolve()
    src = make_project_folder(tmp_path / "work" / "alpha", str(tmp_path / "remote" / "alpha") + "/")
    (tmp_path / "remote" / "alpha").mkdir(parents=True)
    (src / "sub").mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    app = app_module.App(tmp_path / "config" / "projects.toml", screen=FakeScreen())
    app.register(src)
    yield app
    for known in app.projects:
        known.close()


@pytest.fixture
def watched(hub, monkeypatch):
    """The hub's project with watch mode on, short timers, and its remote folder."""
    pytest.importorskip("watchdog")
    if not GNU_RSYNC:
        pytest.skip("needs GNU rsync 3.x")
    monkeypatch.setattr(project_module, "SETTLE_SECONDS", 0.6)
    watched_project = hub.projects[0]
    watched_project.interval = 0.2
    watched_project.start_watch()
    return watched_project, Path(watched_project.remote)


@pytest.fixture
def ssh_localhost():
    """Skip unless this machine accepts `ssh localhost` without a prompt."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip("ssh localhost is not available")
