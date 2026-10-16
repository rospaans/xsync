"""The fixed rows of a view and key input, checked as text at many terminal widths."""

import pytest

from xsync import project as project_module, ui as ui_module
from xsync.output import state as run_state
from helpers import make_panel, visible


@pytest.mark.parametrize("colors", [False, True])
@pytest.mark.parametrize("width", [1, 12, 25, 40, 80, 120, 200])
def test_fixed_rows_never_exceed_terminal_width(width, colors, monkeypatch):
    monkeypatch.setattr(run_state, "force_colors", colors)
    panel = make_panel()
    states = [("watch", project_module.IDLE, "ok", "WATCHING"), ("idle", "3 differences at 15:44", "busy", "XSYNC"),
              ("offline", "offline, 3 queued, retrying", "problem", "OFFLINE")]
    for mode, state, level, label in states:
        panel.mode, panel.state, panel.level, panel.detail = mode, state, level, "28 changes · last 15:44:16"
        bar = visible(panel.bar(width))
        assert len(bar) <= width
        assert bar.startswith(f" {label} "[:width])
    panel.message = "last problem 15:44:16: " + "x" * 300
    for row in panel.header_rows(width):
        assert len(visible(row)) <= width


def test_banner_is_one_row(monkeypatch):
    monkeypatch.setattr(run_state, "force_colors", True)
    panel = make_panel()
    assert "X S Y N C" in visible(panel.banner(200))   # letter-spaced when wide
    assert " XSYNC " in visible(panel.banner(60))       # compact when narrow
    for width in (5, 60, 89, 90, 200):
        assert len(visible(panel.banner(width))) == width
        assert "\n" not in panel.banner(width)


def test_bar_shows_transport_state_and_counter():
    panel = make_panel()
    panel.state, panel.detail = "in sync at 15:52", "28 changes · last 15:44:16"
    bar = panel.bar(200)
    for part in ("XSYNC", "a_project", "SSH", "host:some/long/remote/folder/", "in sync at 15:52", "28 changes"):
        assert part in bar


def test_bar_without_a_remote():
    panel = ui_module.Panel("3 projects", ui_module.OVERVIEW_KEYS)
    panel.state = "2 watching"
    for width in (10, 40, 120):
        bar = panel.bar(width)
        assert len(bar) <= width
    assert "3 projects" in panel.bar(120) and "→" not in panel.bar(120)


@pytest.mark.parametrize("keys, help_text", [
    (ui_module.PROJECT_KEYS, ui_module.KEY_HELP), (ui_module.OVERVIEW_KEYS, ui_module.OVERVIEW_HELP),
])
def test_every_button_has_a_unique_key_and_help(keys, help_text):
    flat = [key for row in keys for key, _ in row]
    assert len(flat) == len(set(flat))
    assert set(flat) <= set(help_text)
    rows = "\n".join(ui_module.Panel("p", keys).key_rows(300))
    for key in flat:
        assert f"[{key}]" in rows


def test_buttons_show_switch_states():
    panel = make_panel()
    rows = "\n".join(panel.key_rows(200))
    assert "Watch off" in rows and "Verbose off" in rows
    panel.switches = {"w": True, "v": True}
    rows = "\n".join(panel.key_rows(200))
    assert "Watch on" in rows and "Verbose on" in rows


@pytest.mark.parametrize("colors", [False, True])
def test_buttons_wrap_into_more_rows_on_a_narrow_terminal(colors, monkeypatch):
    monkeypatch.setattr(run_state, "force_colors", colors)
    panel = make_panel()
    count = sum(len(row) for row in ui_module.PROJECT_KEYS)

    def layout(width):
        return [visible(row) for row in panel.key_rows(width)]

    assert len(layout(200)) == len(ui_module.PROJECT_KEYS)  # all columns
    assert len(layout(60)) == -(-count // 3)           # three columns
    assert len(layout(45)) == -(-count // 2)           # two columns
    assert len(layout(25)) == count                    # one button per line
    for width in (200, 60, 45, 25):
        rows = layout(width)
        text = "\n".join(rows)
        assert all(len(row) <= width for row in rows)
        assert "Reconnect" in text and "Verbose off" in text   # labels stay whole
        assert len({len(row) for row in rows[:-1]}) <= 1       # full rows line up


def test_message_row_is_empty_without_a_message():
    panel = make_panel()
    assert panel.message_row(80) == ""
    panel.message = "something happened"
    assert "something happened" in panel.message_row(80)


@pytest.mark.parametrize("chunk, expected", [
    ("s", ["s"]),
    ("\033[A", ["up"]),
    ("\033[A\033[A\033[B", ["up", "up", "down"]),          # a mouse wheel sends a burst of arrows
    ("\033OA\033[5~\033[6~\033[H\033[F", ["up", "pageup", "pagedown", "home", "end"]),
    ("\033", ["esc"]),
    ("\033[200~", []),                                      # sequences that mean nothing here are dropped
    ("/some/path\n", list("/some/path") + ["\n"]),         # a paste
    ("é", ["é"]),
])
def test_parse_keys(chunk, expected):
    assert ui_module.parse_keys(chunk) == expected


@pytest.mark.parametrize("width", [60, 200])
def test_the_watching_badge_counts_dots_without_moving_the_bar(width, monkeypatch):
    monkeypatch.setattr(run_state, "force_colors", True)
    panel = make_panel()
    panel.mode = "watch"
    banners = []
    for dots in range(ui_module.WATCH_DOTS + 1):
        panel.dots = dots
        banners.append(visible(panel.banner(width)))
    label = "W A T C H I N G" if width >= 90 else "WATCHING"
    dot = " ." if width >= 90 else "."
    for dots, banner in enumerate(banners):
        assert (label + dot * dots + " ") in banner
    # the project name sits in the same column at every step
    assert len({banner.index("a_project") for banner in banners}) == 1
