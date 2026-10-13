"""Shared pieces for the tests: scratch projects, a stand-in terminal, and small checks.

xsync itself is interactive and needs a terminal, so the tests call its
functions directly. Only --help, --version, --init and argument checking run
through the command line (see run_xsync).
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from xsync import rsync as rsync_module
from xsync import ui as ui_module

REPO = Path(__file__).resolve().parent.parent
HOME = str(Path.home())
EXCLUDES = [".xsync.toml", ".xsync.exclude", ".xsync.files", "ignored/"]
RSYNC = [rsync_module.resolve_rsync({})]


def _default_rsync_is_gnu() -> bool:
    try:
        out = subprocess.run([RSYNC[0], "--version"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return rsync_module.is_gnu_rsync3(out)


GNU_RSYNC = _default_rsync_is_gnu()
needs_rsync = pytest.mark.skipif(shutil.which(RSYNC[0]) is None, reason="rsync not available")
needs_gnu_rsync = pytest.mark.skipif(not GNU_RSYNC, reason="needs GNU rsync 3.x")


def run_xsync(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `python -m xsync` without a terminal."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
    return subprocess.run(
        [sys.executable, "-m", "xsync", *args],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
    )


def tree(root: Path) -> set[str]:
    """The files under root, as paths relative to it."""
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def visible(text: str) -> str:
    """Strip terminal color codes."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


class Sandbox:
    """A source folder with a few files and an empty local destination, for calling the rsync functions directly."""

    def __init__(self, tmp_path: Path):
        tmp_path = tmp_path.resolve()
        self.src, self.dst = tmp_path / "src", tmp_path / "dst"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "ignored").mkdir()
        self.dst.mkdir()
        (self.src / "a.txt").write_text("a")
        (self.src / "sub" / "b.txt").write_text("b")
        (self.src / "ignored" / "c.txt").write_text("c")
        self.remote = str(self.dst) + "/"
        self.exclude_file = self.src / ".xsync.exclude"
        self.exclude_file.write_text("\n".join(EXCLUDES) + "\n")

    def sync(self, dry_run: bool = False, rsync: list[str] = RSYNC, remote: str | None = None) -> int:
        return rsync_module.sync_full(self.src, remote or self.remote, self.exclude_file, dry_run, rsync)

    def sync_path(self, rel: str, dry_run: bool = False) -> int:
        return rsync_module.sync_path(rel, self.src, self.remote, self.exclude_file, dry_run, RSYNC)

    def local_files(self) -> set[str]:
        return tree(self.src) - {".xsync.exclude"}


class FakeScreen:
    """Stands in for the terminal: a fixed size, no keys, and writes are collected."""

    def __init__(self, columns=120, lines=40):
        self.columns, self.lines = columns, lines
        self.written = []

    def size(self):
        return os.terminal_size((self.columns, self.lines))

    def write(self, text):
        self.written.append(text)

    def read_key(self, timeout):
        return None

    def suspend(self):
        self.written.append("<suspend>")

    def resume(self):
        self.written.append("<resume>")


def make_project_folder(root: Path, remote: str, extra: str = "") -> Path:
    """Create a folder with a .xsync.toml pointing at remote; returns the resolved folder."""
    root.mkdir(parents=True, exist_ok=True)
    excludes = ", ".join(f'"{pattern}"' for pattern in EXCLUDES)
    (root / ".xsync.toml").write_text(f'remote = "{remote}"\nexclude = [{excludes}]\n{extra}')
    return root.resolve()


def write_config(folder: Path, remote: str):
    (folder / ".xsync.toml").write_text(f'remote = "{remote}"\nexclude = []\n')


def make_panel():
    panel = ui_module.Panel("a_project", ui_module.PROJECT_KEYS, "host:some/long/remote/folder/", "ssh")
    panel.switches = {"w": False, "v": False}
    return panel


def fake_editor(tmp_path: Path, monkeypatch, new_remote: str | None):
    """Make $EDITOR a script that sets the remote line (or changes nothing) and records its argument."""
    script = tmp_path / "fake-editor.sh"
    change = f"sed -i.bak 's|^remote = .*|remote = \"{new_remote}\"|' \"$1\" && rm -f \"$1.bak\"" if new_remote else "true"
    script.write_text(f'#!/bin/sh\necho "$1" >> "{tmp_path}/edited.log"\n{change}\n')
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    return tmp_path / "edited.log"


def run_ticks(project, seconds: float):
    """Let a watched project work for a while, as the main loop would."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        project.tick(time.monotonic())
        time.sleep(0.05)


def run_ticks_until(project, done, timeout: float):
    """Let a watched project work until done() is true, or the timeout passes."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and not done():
        project.tick(time.monotonic())
        time.sleep(0.05)


def shown_lines(hub):
    """The list lines in the last thing drawn."""
    return re.findall(r"line \d+", visible(hub.screen.written[-1]))
