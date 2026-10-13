#!/usr/bin/env python3
"""Regenerate the pictures in the README from xsync's own drawing code.

    python3 docs/make_pictures.py

The pictures show placeholder projects, so they contain nothing from the
machine they are made on. Run this again after changing how xsync looks.
"""

import os
import re
import shutil
import sys
import tempfile
from html import escape
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from xsync.app import App  # noqa: E402
from xsync.output import paint, state  # noqa: E402
from xsync.project import IDLE  # noqa: E402
from xsync.ui import PROJECT_KEYS, Panel  # noqa: E402

COLUMNS = 132
CELL_WIDTH, LINE_HEIGHT, PADDING, FONT_SIZE = 8.4, 21, 18, 14
PAGE, DEFAULT_TEXT = "#15181e", "#d0d0d0"
BASIC_COLORS = {31: "#e06c75", 32: "#5fd75f", 33: "#e5c07b", 34: "#61afef", 35: "#c678dd", 36: "#56b6c2",
                37: "#d0d0d0", 97: "#ffffff"}


class FakeScreen:
    """A terminal of a fixed size that shows nothing."""

    def size(self):
        return os.terminal_size((COLUMNS, 40))

    def write(self, text):
        pass

    def read_key(self, timeout):
        return None


def project_view() -> list[str]:
    """One project in watch mode, with a few changes in its list."""
    panel = Panel("my_project", PROJECT_KEYS, "myhost:projects/my_project/", "ssh")
    panel.mode, panel.switches = "watch", {"w": True, "v": False}
    panel.state, panel.level, panel.detail = IDLE, "ok", "7 changes · last 15:26:41"
    changes = [
        ("15:10:08", "~ src/train.py", "blue"), ("15:10:08", "+ src/metrics.py", "green"),
        ("15:10:08", "- src/old_utils.py", "red"), ("15:24:51", "~ README.md", "blue"),
        ("15:26:27", "+ notes/", "green"), ("15:26:27", "+ notes/draft.md", "green"),
        ("15:26:41", "~ src/model.py", "blue"),
    ]
    return panel.header_rows(COLUMNS) + [f"{when}  " + paint(text, color) for when, text, color in changes] + [""]


def overview() -> list[str]:
    """The overview, from three throwaway project folders with placeholder remotes."""
    work = Path(tempfile.mkdtemp())
    try:
        app = App(work / "projects.toml", screen=FakeScreen())
        for name in ("my_project", "thesis_figures", "data_pipeline"):
            (work / name).mkdir()
            (work / name / ".xsync.toml").write_text(f'remote = "myhost:projects/{name}/"\n')
            app.register(work / name)
        states = {"my_project": (IDLE, "ok", 7), "thesis_figures": ("3 differences at 15:20", "busy", 0),
                  "data_pipeline": ("syncing…", "busy", 42)}
        for project in app.projects:
            project.panel.state, project.panel.level, project.changes = states[project.name]
        watching = [project for project in app.projects if project.changes]
        for project in watching:
            project.observer = object()  # counted as watching in the summary
        app.selected = [project.name for project in app.projects].index("my_project")
        app.summarize()
        rows = app.panel.header_rows(COLUMNS) + app.table_rows(COLUMNS) + [""]
        for project in watching:
            project.observer = None
        return rows
    finally:
        shutil.rmtree(work)


def color256(number: int) -> str:
    if number < 16:
        return ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
                "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"][number]
    if number < 232:
        number -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (levels[number // 36], levels[number // 6 % 6], levels[number % 6])
    gray = 8 + (number - 232) * 10
    return "#%02x%02x%02x" % (gray, gray, gray)


def runs(line: str):
    """Yield (text, foreground, background, bold, dim) for each styled run in a line."""
    foreground, background, bold, dim = DEFAULT_TEXT, None, False, False
    for part in re.split(r"(\033\[[0-9;]*m)", line):
        if part.startswith("\033["):
            codes = [int(code) for code in part[2:-1].split(";") if code] or [0]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code == 0:
                    foreground, background, bold, dim = DEFAULT_TEXT, None, False, False
                elif code == 1:
                    bold = True
                elif code == 2:
                    dim = True
                elif code in (38, 48) and codes[index + 1:index + 2] == [5]:
                    if code == 38:
                        foreground = color256(codes[index + 2])
                    else:
                        background = color256(codes[index + 2])
                    index += 2
                elif code in BASIC_COLORS:
                    foreground = BASIC_COLORS[code]
                index += 1
        elif part:
            yield part, foreground, background, bold, dim


def to_svg(title: str, lines: list[str]) -> str:
    width, height = COLUMNS * CELL_WIDTH + 2 * PADDING, len(lines) * LINE_HEIGHT + 2 * PADDING
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
           f'width="{width:.0f}" height="{height:.0f}" '
           f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, \'DejaVu Sans Mono\', monospace" '
           f'font-size="{FONT_SIZE}">',
           f"<title>{escape(title)}</title>",
           f'<rect width="100%" height="100%" rx="8" fill="{PAGE}"/>']
    for row, line in enumerate(lines):
        column = 0
        y = PADDING + row * LINE_HEIGHT
        for text, foreground, background, bold, dim in runs(line):
            x, run_width = PADDING + column * CELL_WIDTH, len(text) * CELL_WIDTH
            if background:
                out.append(f'<rect x="{x:.1f}" y="{y}" width="{run_width + 0.5:.1f}" height="{LINE_HEIGHT}" fill="{background}"/>')
            if text.strip():
                style = f'fill="{foreground}"' + (' font-weight="bold"' if bold else "") + (' opacity="0.55"' if dim else "")
                out.append(f'<text x="{x:.1f}" y="{y + 15}" textLength="{run_width:.1f}" lengthAdjust="spacingAndGlyphs" '
                           f'xml:space="preserve" {style}>{escape(text)}</text>')
            column += len(text)
    return "\n".join(out + ["</svg>"]) + "\n"


def main():
    state.force_colors = True
    pictures = {"overview.svg": ("The overview of all projects", overview()),
                "session.svg": ("One project in watch mode", project_view())}
    for name, (title, lines) in pictures.items():
        (REPO / "docs" / name).write_text(to_svg(title, lines))
        print(f"docs/{name}: {len(lines)} rows")


if __name__ == "__main__":
    main()
