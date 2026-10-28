"""Drawing and key input: the fixed rows at the top of a view, and the terminal itself."""

import os
import re
import select
import shutil
import signal
import sys
from collections import deque

from .output import colors_on, paint


# Colors of the status bar (256-color codes)
BAR_BACKGROUND = "48;5;236"


BAR_BADGE = {"watch": "1;38;5;16;48;5;78", "idle": "1;38;5;16;48;5;75", "offline": "1;38;5;16;48;5;176"}


BAR_LABEL = {"watch": "WATCHING", "idle": "XSYNC", "offline": "OFFLINE"}


# In watch mode the badge counts dots up and back: WATCHING, WATCHING., WATCHING.., WATCHING...
WATCH_DOTS = 3


# The transport tag (SSH or LOCAL) in front of the remote
BAR_TRANSPORT = "1;38;5;255;48;5;61"


# The change counter on the right
BAR_TAG = "1;38;5;255;48;5;240"


BAR_DOT = {"ok": "38;5;78", "busy": "38;5;221", "problem": "38;5;176"}


# The highlighted row in the project overview
ROW_SELECTED = "48;5;238"


# Buttons in a project's view: what you do, and what you look at
PROJECT_KEYS = [
    [("s", "Sync"), ("m", "Mirror"), ("w", "Watch"), ("c", "Compare"), ("f", "Path"), ("r", "Reconnect")],
    [("i", "Info"), ("p", "Problems"), ("e", "Edit"), ("l", "Clear"), ("v", "Verbose"), ("b", "Back")],
    [("h", "Help"), ("q", "Quit")],
]


# Buttons in the overview; s, w and c act on the highlighted project
OVERVIEW_KEYS = [
    [("o", "Open"), ("s", "Sync"), ("m", "Mirror"), ("w", "Watch"), ("c", "Compare"), ("a", "Add")],
    [("e", "Edit"), ("x", "Forget"), ("v", "Verbose"), (",", "Settings"), ("h", "Help")],
    [("q", "Quit")],
]


# Buttons on the settings screen; u asks for confirmation before it changes anything
SETTINGS_KEYS = [
    [("c", "Check for updates"), ("u", "Update"), ("a", "Check at start"), ("b", "Back")],
    [("h", "Help"), ("q", "Quit")],
]


SETTINGS_HELP = {
    "c": "check GitHub for a newer version now",
    "u": "update: git pull in the checkout xsync was installed from, then install.sh; asks you to type yes first",
    "a": "check at start on/off: look for a newer version each time xsync starts",
    "b": "back to the overview (Esc and ← work too)",
    "h": "this help",
    "q": "quit (Ctrl+C works too)",
}


KEY_HELP = {
    "s": "sync: send what changed here since the last sync; quick, as it does not read the server",
    "m": "mirror (full sync): compare every file with the server and make it match; slower, catches changes made there",
    "w": "watch mode on/off: on syncs what changed since the last sync, then sends changes as you make them",
    "c": "compare with the server, like git status; changes nothing",
    "f": "sync one file or folder: type, paste or drag a path, then Enter",
    "r": "check the connection now and send what was queued",
    "b": "back to the overview of all projects (Esc and ← work too); during a sync, Esc cancels it",
    "i": "info: project, remote, host, rsync and flags in use",
    "p": "problems: list recorded warnings and errors, clear the problem line",
    "e": "edit this project's .xsync.toml in your editor ($VISUAL, $EDITOR, or nano)",
    "l": "clear this project's list (nothing else changes)",
    "v": "verbose on/off: show rsync's raw output",
    "h": "this help",
    "q": "quit (Ctrl+C works too)",
}


OVERVIEW_HELP = {
    "↑ ↓": "move the highlight (j and k work too)",
    "o": "open the highlighted project (Enter and → work too)",
    "s": "sync the highlighted project: send what changed here since the last sync",
    "m": "mirror (full sync) the highlighted project: compare every file with the server",
    "w": "watch mode on/off for the highlighted project; it keeps running while you look elsewhere",
    "c": "compare the highlighted project with the server; changes nothing",
    "a": "add a project: type, paste or drag its folder (Tab completes), then Enter; a folder without a .xsync.toml gets one",
    "e": "edit the highlighted project's .xsync.toml in your editor ($VISUAL, $EDITOR, or nano)",
    "x": "forget the highlighted project: it leaves this list, and no file anywhere is touched",
    "v": "verbose on/off: show rsync's raw output",
    ",": "settings: check for a newer version of xsync, and update it",
    "h": "this help",
    "q": "quit (Ctrl+C works too)",
}


# Buttons: a light key cap next to a darker label; the cap is colored to show state
BUTTON_CAP, BUTTON_CAP_ON, BUTTON_CAP_ALERT = "1;38;5;16;48;5;252", "1;38;5;16;48;5;78", "1;38;5;16;48;5;176"


BUTTON_LABEL = "38;5;255;48;5;238"


BUTTON_GAP = 2


def styled(segments: list[tuple[str, str]], background: str = "") -> str:
    """Join (text, color code) pairs into one string; plain text when colors are off."""
    if not colors_on():
        return "".join(text for text, _ in segments)
    out = ""
    for text, code in segments:
        codes = ";".join(part for part in ("0", background, code) if part)
        out += f"\033[{codes}m{text}"
    return out + "\033[0m"


def segments_length(segments: list[tuple[str, str]]) -> int:
    return sum(len(text) for text, _ in segments)


def shorten(text: str, length: int) -> str:
    """Cut text to at most length characters, ending in … where it was cut, not in a stray comma or dot."""
    if len(text) <= length:
        return text
    return text[:max(length - 1, 0)].rstrip(" ,·…") + "…" if length > 0 else ""


def clip_segments(segments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Cut segments off at width characters, as a last resort on a tiny terminal."""
    clipped = []
    for text, code in segments:
        room = width - segments_length(clipped)
        if room <= 0:
            break
        clipped.append((text[:room], code))
    return clipped


def spread(left: list[tuple[str, str]], right: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Put left and right segments at the two ends of a row of exactly width characters."""
    gap = max(width - segments_length(left) - segments_length(right), 0)
    row = clip_segments(left + [(" " * gap, "")] + right, width)
    return row + [(" " * (width - segments_length(row)), "")]


def choice_rows(names: list[str], width: int, max_rows: int) -> list[str]:
    """Names in columns, read top to bottom like ls: as many columns as fit the width.

    When they do not all fit in max_rows, the last row says how many are left out.
    """
    if not names or max_rows < 1:
        return []
    column_width = min(max(len(name) for name in names) + 2, max(width - 2, 1))
    columns = max(1, (width - 2) // column_width)
    rows = -(-len(names) // columns)
    left_out = 0
    if rows > max_rows:
        rows = max(max_rows - 1, 1)
        left_out = len(names) - rows * columns
    grid = []
    for row in range(rows):
        cells = [names[index] for index in range(row, min(len(names), rows * columns), rows)]
        grid.append(" " + "".join(cell[:column_width - 1].ljust(column_width) for cell in cells).rstrip())
    if left_out > 0:
        grid.append(paint(f" … and {left_out} more; type more letters to narrow them down", "dim"))
    return grid


class Panel:
    """The rows fixed at the top of a view: a status bar, a grid of buttons and a message line."""

    def __init__(self, name: str, keys: list[list[tuple[str, str]]], remote: str = "", transport: str = ""):
        self.name, self.remote, self.transport = name, remote, transport
        self.keys = keys
        self.mode, self.state, self.level, self.detail = "idle", "starting…", "busy", ""
        self.switches: dict[str, bool] = {}   # on/off buttons, by key
        self.highlights: set[str] = set()     # buttons to draw attention to, such as an update to install
        self.dots = 0                         # the step of the watch-mode animation, 0 to WATCH_DOTS
        self.message, self.message_color = "", "dim"

    def badge_label(self, spaced: bool = False) -> str:
        """The mode name; in watch mode with its dots, padded so the badge keeps one width."""
        label = BAR_LABEL[self.mode]
        full = label + "." * WATCH_DOTS if self.mode == "watch" else label
        if self.mode == "watch":
            label += "." * self.dots
        if spaced:
            label, full = " ".join(label), " ".join(full)
        return label.ljust(len(full))

    def bar(self, width: int, badge: tuple[str, str] | None = None) -> str:
        """The mode badge, what is synced where, and on the right the current state."""
        badge = badge or (f" {self.badge_label()} ", BAR_BADGE[self.mode])
        left = [badge, (f"  {self.name}", "1;97")]
        if self.remote:
            left += [("  →  ", "38;5;250"), (f" {self.transport.upper()} ", BAR_TRANSPORT),
                     (f" {self.remote}", "38;5;250")]
        right = [("●", BAR_DOT[self.level]), (f" {self.state}", "38;5;255")]
        # detail is "N changes · last HH:MM:SS": the count as a tag, the time after it
        count, _, last = self.detail.partition(" · ")
        extras = []
        if count:
            extras += [("   ", ""), (f" {count} ", BAR_TAG)]
        if last:
            extras.append((f"  {last}", "38;5;250"))
        right += extras + [(" ", "")]

        # Too narrow: drop the time, then the count, then shorten the remote, then the state
        while extras and segments_length(left) + 3 + segments_length(right) > width:
            extras.pop()
            if extras and extras[-1][0] == "   ":
                extras.pop()
            right = right[:2] + extras + [(" ", "")]
        if self.remote:
            room = width - segments_length(left[:4]) - 3 - segments_length(right)
            if room < 8:
                left = left[:2]  # a remote cut down to a few letters says nothing; the info screen has it
            else:
                left[4] = (shorten(left[4][0], room), left[4][1])
        over = segments_length(left) + 3 + segments_length(right) - width
        if over > 0:
            keep = len(right[1][0]) - over
            right[1] = (shorten(right[1][0], keep) if keep >= 3 else "", right[1][1])
        while len(left) > 1 and segments_length(left) + segments_length(right) > width:
            left.pop()
        return styled(spread(left, right, width), BAR_BACKGROUND)

    def key_rows(self, width: int) -> list[str]:
        """The keys as a grid of equal-width buttons.

        As many columns as fit with full labels; a narrower terminal gets fewer
        columns and more rows, down to one button per line.
        """
        plain = not colors_on()
        keys = [item for row in self.keys for item in row]
        cap_width = 4 if plain else 3   # "[s] " or " s "
        longest = max(len(label) + (len(" off") if key in self.switches else 0) for key, label in keys)
        label_width = longest + (0 if plain else 2)
        cell_width = cap_width + label_width + BUTTON_GAP
        columns = max(1, min(len(self.keys[0]), (width - 1) // cell_width))
        if cell_width > width - 1:  # narrower than one button: cut the label
            label_width = max(width - 1 - cap_width - BUTTON_GAP, 0)
        rows = []
        for start in range(0, len(keys), columns):
            segments = [(" ", "")]
            for key, label in keys[start:start + columns]:
                cap = BUTTON_CAP
                if key in self.switches:
                    label += " on" if self.switches[key] else " off"
                    cap = BUTTON_CAP_ON if self.switches[key] else BUTTON_CAP
                elif key == "r" and self.mode == "offline":
                    cap = BUTTON_CAP_ALERT
                elif key in self.highlights:
                    cap = BUTTON_CAP_ON
                text = (label if plain else f" {label}")[:label_width].ljust(label_width)
                cell = [(f"[{key}] " if plain else f" {key} ", cap), (text, BUTTON_LABEL), (" " * BUTTON_GAP, "")]
                if segments_length(segments) + segments_length(cell) > width:
                    break  # not even the key cap fits
                segments += cell
            rows.append(styled(segments))
        return rows

    def banner(self, width: int) -> str:
        """The status bar, with the mode name letter-spaced on a wide badge when there is room."""
        text = f"   {self.badge_label(spaced=True)}   " if width >= 90 else f" {self.badge_label()} "
        return self.bar(width, (text, BAR_BADGE[self.mode]))

    def message_row(self, width: int) -> str:
        """The latest problem or the input prompt; empty when there is nothing to say."""
        return paint(f" {self.message}"[:max(width - 1, 0)], self.message_color) if self.message else ""

    def header_rows(self, width: int) -> list[str]:
        """Everything that stays fixed at the top, one string per terminal row."""
        banner = [self.banner(width) if colors_on() else self.bar(width)]
        rule = paint("─" * max(width - 1, 0), "dim")
        return [*banner, "", *self.key_rows(width), self.message_row(width), rule]


ESCAPE_KEYS = {"\033[A": "up", "\033[B": "down", "\033[C": "right", "\033[D": "left",
               "\033OA": "up", "\033OB": "down", "\033OC": "right", "\033OD": "left",
               "\033[5~": "pageup", "\033[6~": "pagedown", "\033[H": "home", "\033[F": "end",
               "\033[1~": "home", "\033[4~": "end", "\033OH": "home", "\033OF": "end", "\033": "esc"}


def parse_keys(chunk: str) -> list[str]:
    """Split what the terminal sent into keys: characters, and names for arrows and paging keys.

    Several keys can arrive at once (a paste, or a mouse wheel sending a burst
    of arrows). Escape sequences that mean nothing here are dropped.
    """
    keys = []
    for token in re.findall(r"\033\[[0-9;]*[A-Za-z~]|\033O[A-Za-z]|\033|.", chunk, re.DOTALL):
        if token.startswith("\033"):
            if token in ESCAPE_KEYS:
                keys.append(ESCAPE_KEYS[token])
        else:
            keys.append(token)
    return keys


class Screen:
    """The terminal: single key presses without Enter, and raw writes for drawing."""

    def __init__(self):
        self._typed: deque[str] = deque()
        self._terminal_settings = None

    def size(self) -> os.terminal_size:
        return shutil.get_terminal_size()

    def write(self, text: str):
        sys.stdout.write(text)
        sys.stdout.flush()

    def read_key(self, timeout: float) -> str | None:
        """Wait up to timeout seconds for a key press.

        Returns the character, or a name such as up, down, pageup, home or esc.
        """
        if not self._typed:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            self._typed.extend(parse_keys(os.read(sys.stdin.fileno(), 4096).decode("utf-8", "ignore")))
        return self._typed.popleft() if self._typed else None

    def __enter__(self):
        import termios
        self._terminal_settings = termios.tcgetattr(sys.stdin.fileno())
        self.resume()
        return self

    def resume(self):
        """Take the terminal: single key presses, no echo (Ctrl+C still works), hidden cursor.

        Uses the alternate screen, so what was in the terminal comes back on
        exit, and asks for the mouse wheel to arrive as arrow keys, which
        scrolls a project's list without taking over mouse selection.
        """
        import tty
        tty.setcbreak(sys.stdin.fileno())
        self.write("\033[?1049h\033[?1007h\033[?25l")

    def suspend(self):
        """Hand the terminal to another program, such as an editor."""
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._terminal_settings)
        self.write("\033[r\033[?25h\033[?1007l\033[?1049l")

    def __exit__(self, *exc):
        import termios
        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._terminal_settings)
        # Reset the scroll region, show the cursor, leave the alternate screen, clear the tab title
        self.write("\033[r\033[?25h\033[?1007l\033[?1049l\033]0;\007")
        return False
