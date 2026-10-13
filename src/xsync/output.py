"""Where output goes: the shared run state, colors, and the lines that end up in a project's list."""

import os
import sys
from datetime import datetime


class State:
    """What the parts of xsync share while it runs.

    rsync is only ever run for one project at a time, so "the active project"
    is a single place: whoever starts an operation sets `output` first.
    """

    def __init__(self):
        # Summarize rsync's output instead of passing it through. Needs GNU rsync 3.x;
        # switched off by --verbose, the v key, or another rsync flavor.
        self.pretty = False
        # Number of new, updated and deleted paths sent so far, and the counts of the last rsync run
        self.changes = 0
        self.last_counts = {"new": 0, "updated": 0, "deleted": 0}
        # Where lines and problems go; a project takes this over while its rsync runs
        self.output = Printer()
        # None follows the terminal; True or False overrides it
        self.force_colors: bool | None = None


COLORS = {
    # 256-color blue and red: the basic ones are too dark on dark terminal themes
    "green": "32", "blue": "38;5;75", "red": "38;5;203", "yellow": "33", "magenta": "35", "dim": "2", "bold": "1",
}


def colors_on() -> bool:
    if state.force_colors is not None:
        return state.force_colors
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text: str, color: str | None) -> str:
    """Color text for a terminal; plain when piped or when NO_COLOR is set."""
    if color is None or not colors_on():
        return text
    return f"\033[{COLORS[color]}m{text}\033[0m"


class Printer:
    """Where output lines go when no project is active: straight to the terminal."""

    def line(self, text: str):
        print(text, flush=True)

    def problem(self, text: str):
        pass

    def waiting(self, seconds: float) -> bool:
        """Called a few times a second while rsync runs. Return True to cancel it."""
        return False


state = State()


def emit(text: str, color: str | None = None, note: str = ""):
    """Add one time-stamped line to the active project's list."""
    line = f"{datetime.now():%H:%M:%S}  " + paint(text, color)
    state.output.line(line + (paint(f"  ({note})", "dim") if note else ""))


def warn(text: str, color: str = "yellow"):
    """Add a warning or error line, and record it as a problem."""
    emit(f"! {text}", color)
    state.output.problem(text)


def clock() -> str:
    return f"{datetime.now():%H:%M:%S}"
