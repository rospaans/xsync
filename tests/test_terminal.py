"""xsync as a whole, driven through a pseudo-terminal: real key presses, real drawing, real file watcher.

The remotes are local folders, so nothing leaves the machine. These tests are
slower than the rest (a second or two each) because they wait for the screen.
"""

import os
import re
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import GNU_RSYNC, REPO, make_project_folder, tree

pty = pytest.importorskip("pty")
fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")
pytest.importorskip("watchdog")

pytestmark = pytest.mark.skipif(not GNU_RSYNC, reason="needs GNU rsync 3.x")

ARROW_UP, ARROW_DOWN, PAGE_UP, END, ESC = "\033[A", "\033[B", "\033[5~", "\033[F", "\033"


class Terminal:
    """Runs `python -m xsync` in a pseudo-terminal and reads what it draws."""

    def __init__(self, cwd: Path, config_home: Path, editor: Path | None = None, rows: int = 40, columns: int = 130):
        self.master, slave = pty.openpty()
        self.resize(rows, columns, descriptor=slave)
        env = {**os.environ, "XDG_CONFIG_HOME": str(config_home), "TERM": "xterm-256color",
               "PYTHONUTF8": "1", "PYTHONPATH": str(REPO / "src")}
        env.pop("NO_COLOR", None)
        env.pop("VISUAL", None)
        if editor:
            env["EDITOR"] = str(editor)
        # A test runner started in the background has Ctrl+C switched off; a terminal never does
        self.process = subprocess.Popen([sys.executable, "-m", "xsync"], cwd=cwd, env=env,
                                        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        os.close(slave)
        self.raw = b""
        self.mark = 0

    def resize(self, rows: int, columns: int, descriptor: int | None = None):
        fcntl.ioctl(self.master if descriptor is None else descriptor, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0))

    def _read(self, seconds: float):
        import select
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    self.raw += os.read(self.master, 1 << 16)
                except OSError:
                    return

    @property
    def text(self) -> str:
        """Everything drawn since the last key press, without the terminal control codes."""
        raw = self.raw[self.mark:].decode("utf-8", "replace")
        raw = re.sub(r"\033\[[0-9;]*m", "", raw)              # colors: drop, so words stay together
        return re.sub(r"\033\][^\007]*\007|\033\[[0-9;?]*[A-Za-z]|\0337|\0338", " ", raw)   # moves: a gap

    def press(self, keys: str):
        """Send keys; `text` afterwards only holds what was drawn in response."""
        self._read(0.1)
        self.mark = len(self.raw)
        os.write(self.master, keys.encode())

    def expect(self, *needles: str, timeout: float = 15.0) -> str:
        """Wait until every needle has been drawn since the last key press."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._read(0.1)
            if all(needle in self.text for needle in needles):
                return self.text
        missing = [needle for needle in needles if needle not in self.text]
        pytest.fail(f"never drawn: {missing}\n--- drawn since the last key press ---\n{self.text[-3000:]}")

    def wait_for_exit(self, timeout: float = 10.0) -> int:
        """Wait for xsync to end, reading what it still draws so it never blocks on a full terminal."""
        end = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < end:
            self._read(0.1)
        if self.process.poll() is None:
            pytest.fail(f"xsync did not exit\n--- drawn since the last key press ---\n{self.text[-2000:]}")
        return self.process.returncode

    def quit(self) -> int:
        self.press("q")
        try:
            return self.wait_for_exit()
        finally:
            self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        try:
            os.close(self.master)
        except OSError:
            pass


@pytest.fixture
def world(tmp_path):
    """Two projects (alpha, beta) with local remotes, and a place for xsync's own config."""
    root = tmp_path.resolve()
    projects = {}
    for name in ("alpha", "beta"):
        remote = root / "remote" / name
        remote.mkdir(parents=True)
        source = make_project_folder(root / "work" / name, f"{remote}/", "interval = 0.3\n")
        (source / "sub").mkdir()
        (source / "a.txt").write_text("a")
        (source / "sub" / "b.txt").write_text("b")
        projects[name] = (source, remote)
    terminals = []

    def start(cwd: Path, **options) -> Terminal:
        terminal = Terminal(cwd, root / "config", **options)
        terminals.append(terminal)
        return terminal

    yield root, projects, start
    for terminal in terminals:
        terminal.close()


def test_starting_shows_the_overview_and_runs_nothing(world):
    root, projects, start = world
    source, remote = projects["alpha"]
    terminal = start(source)
    drawn = terminal.expect("X S Y N C", "1 project", "alpha", "LOCAL", "idle, nothing checked yet", "Forget")
    assert "not synced" not in drawn and "syncing" not in drawn
    terminal._read(1)
    assert tree(remote) == set()                              # nothing was sent just by starting
    assert terminal.quit() == 0
    assert (root / "config" / "xsync" / "projects.toml").read_text().count(str(source)) == 1


def test_compare_sync_and_watch_in_a_project(world):
    root, projects, start = world
    source, remote = projects["alpha"]
    terminal = start(source)
    terminal.expect("alpha")

    terminal.press("o")
    terminal.expect("Reconnect", "Back")
    terminal.press("c")
    terminal.expect("+ a.txt", "not synced", "3 difference(s)")
    assert tree(remote) == set()                              # a compare changes nothing

    terminal.press("s")
    terminal.expect("+ sub/b.txt", "in sync at")
    assert tree(remote) == {"a.txt", "sub/b.txt"}

    terminal.press("w")
    terminal.expect("W A T C H", "in sync, watching for changes")
    time.sleep(0.5)
    terminal.press("")                                        # only what the watcher causes from here on
    (source / "new.txt").write_text("n")
    (source / "a.txt").write_text("changed")
    terminal.expect("+ new.txt", "~ a.txt")
    (source / "sub" / "b.txt").unlink()
    terminal.expect("- sub/b.txt")
    assert tree(remote) == {"a.txt", "new.txt"} and (remote / "a.txt").read_text() == "changed"

    terminal.press("i")
    terminal.expect("full sync", "allowed", "rsync")
    terminal.press("h")
    terminal.expect("this help")
    terminal.press("b")
    terminal.expect("1 project", "1 watching")
    assert terminal.quit() == 0
    assert tree(source) == {".xsync.toml", "a.txt", "new.txt"}   # no temp files left in the project


def test_a_project_keeps_watching_while_the_overview_is_shown(world):
    root, projects, start = world
    for source, _ in projects.values():                       # make both known
        assert start(source).quit() == 0
    source, remote = projects["beta"]
    terminal = start(root)                                    # not inside any project
    terminal.expect("2 projects", "alpha", "beta")

    terminal.press(ARROW_DOWN)
    terminal.expect("› beta")
    terminal.press("w")
    terminal.expect("1 watching", "Press o to see its list")
    assert tree(remote) == {"a.txt", "sub/b.txt"}             # switching watch on did a full sync
    terminal.press(ARROW_UP)
    terminal.expect("› alpha")
    (source / "from-the-overview.txt").write_text("x")
    deadline = time.monotonic() + 10
    while "from-the-overview.txt" not in tree(remote) and time.monotonic() < deadline:
        terminal._read(0.2)
    assert "from-the-overview.txt" in tree(remote)            # beta synced while alpha was highlighted
    assert tree(projects["alpha"][1]) == set()                # and alpha was left alone

    terminal.press(ARROW_DOWN + "o")
    terminal.expect("+ from-the-overview.txt")                # its list was kept and is shown on opening
    assert terminal.quit() == 0


def test_adding_a_folder_creates_a_config_and_opens_the_editor(world):
    root, projects, start = world
    fresh = root / "work" / "gamma"
    fresh.mkdir()
    (fresh / "g.txt").write_text("g")
    gamma_remote = root / "remote" / "gamma"
    gamma_remote.mkdir()
    editor = root / "editor.sh"
    editor.write_text(f"#!/bin/sh\nsed -i.bak 's|^remote = .*|remote = \"{gamma_remote}/\"|' \"$1\" && rm -f \"$1.bak\"\n")
    editor.chmod(0o755)

    terminal = start(root, editor=editor)
    terminal.expect("No projects yet")
    terminal.press("a")
    terminal.expect("project folder to add")
    terminal.press(str(fresh) + "\n")                         # the prompt starts empty
    terminal.expect("1 project", "gamma", "Created")
    assert f'remote = "{gamma_remote}/"' in (fresh / ".xsync.toml").read_text()

    terminal.press("s")
    terminal.expect("in sync at")
    assert tree(gamma_remote) == {"g.txt"}
    terminal.press("x")
    terminal.expect("No projects yet", "no file was touched")
    assert tree(gamma_remote) == {"g.txt"} and (fresh / ".xsync.toml").exists()
    assert terminal.quit() == 0


def test_scrolling_back_and_resizing(world):
    root, projects, start = world
    source, _ = projects["alpha"]
    terminal = start(source, rows=24)
    terminal.expect("alpha")
    terminal.press("o")
    terminal.expect("Back")
    for _ in range(3):                                        # more lines than fit on screen
        terminal.press("h")
        terminal.expect("this help")
    terminal.press(PAGE_UP)
    terminal.expect("scrolled back")
    terminal.press(ARROW_UP + ARROW_UP + ARROW_DOWN)          # a burst, as a mouse wheel sends
    terminal.expect("scrolled back")
    terminal.press(END)
    terminal.expect("this help")
    assert "scrolled back" not in terminal.text.split("this help")[-1]

    terminal.press("")
    terminal.resize(30, 60)                                   # narrower: the buttons wrap into more rows
    terminal.process.send_signal(signal.SIGWINCH)
    terminal.expect("XSYNC", "Reconnect", "Quit")
    terminal.resize(40, 130)
    terminal.process.send_signal(signal.SIGWINCH)
    terminal.expect("X S Y N C", "Reconnect")
    assert terminal.quit() == 0


def test_a_second_xsync_is_turned_away(world):
    root, projects, start = world
    source, _ = projects["alpha"]
    first = start(source)
    first.expect("alpha")
    second = start(source)
    second.expect("already running in another terminal")
    assert second.wait_for_exit() == 1
    assert first.process.poll() is None                       # the first one carries on
    first.press("h")
    first.expect("highlight")
    assert first.quit() == 0
    third = start(source)                                     # and once it has ended, xsync starts again
    third.expect("alpha")
    assert third.quit() == 0


def test_unknown_keys_and_ctrl_c(world):
    root, projects, start = world
    source, remote = projects["alpha"]
    terminal = start(source)
    terminal.expect("alpha")
    terminal.press("zZ9" + ESC)                               # keys that mean nothing in the overview
    terminal._read(0.5)
    assert terminal.process.poll() is None and tree(remote) == set()
    terminal.process.send_signal(signal.SIGINT)               # what Ctrl+C does in a real terminal
    assert terminal.wait_for_exit() == 0
