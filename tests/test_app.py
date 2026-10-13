"""The hub: the overview, opening and leaving a project, adding, editing and forgetting projects."""

import tomllib
from collections import deque
from pathlib import Path

import pytest

from xsync import app as app_module, config as config_module, output as output_module
from xsync.output import state as run_state
from helpers import FakeScreen, fake_editor, make_project_folder, needs_gnu_rsync, shown_lines, tree, visible


def test_projects_are_remembered_once_and_can_be_removed(hub, tmp_path):
    alpha = hub.projects[0]
    assert hub.register(alpha.root) is alpha            # already known
    beta = hub.register(make_project_folder(tmp_path / "work" / "beta", "host:projects/beta/"))
    assert [project.name for project in hub.projects] == ["alpha", "beta"]

    again = app_module.App(hub.registry, screen=FakeScreen())  # a new run sees the same list
    assert [project.root for project in again.projects] == [alpha.root, beta.root]

    hub.remove(beta)
    assert config_module.load_registry(hub.registry) == [alpha.root]
    assert (beta.root / ".xsync.toml").exists()          # only the list entry is gone


def test_the_screen_stays_alive_while_rsync_runs(hub):
    project = hub.projects[0]
    project.panel.state = "syncing…"
    assert hub.while_waiting(project, 1.0) is False and project.panel.state == "syncing…"
    assert hub.while_waiting(project, 12.4) is False
    assert project.panel.state == "syncing…  ·  12s, Esc cancels"
    hub.while_waiting(project, 13.2)
    assert project.panel.state == "syncing…  ·  13s, Esc cancels"   # replaced, not appended
    keys = iter(["up", "esc", None])
    hub.screen.read_key = lambda timeout: next(keys)
    assert hub.while_waiting(project, 14) is True          # Esc asks for the sync to be cancelled


def test_only_the_project_on_screen_prints_its_lines(hub, capsys):
    project = hub.projects[0]
    hub.activate(project)
    output_module.emit("hidden line")
    assert capsys.readouterr().out == ""                  # the overview is showing
    hub.open(project)
    output_module.emit("visible line")
    assert "visible line" in capsys.readouterr().out
    assert [line.split("  ", 1)[1] for line in project.log] == ["hidden line", "visible line"]


def test_opening_a_project_replays_the_end_of_its_list(hub):
    project = hub.projects[0]
    hub.activate(project)
    for number in range(100):
        output_module.emit(f"line {number}")
    hub.open(project)
    drawn = hub.screen.written[-1]
    assert "line 99" in drawn and "line 0\n" not in drawn  # only what fits below the fixed rows
    hub.back()
    assert hub.view is None and "\033[r" in hub.screen.written[-1]


@pytest.mark.parametrize("colors", [False, True])
@pytest.mark.parametrize("width", [1, 20, 60, 120, 200])
def test_overview_rows_fit_and_mark_the_highlighted_project(hub, tmp_path, monkeypatch, width, colors):
    monkeypatch.setattr(run_state, "force_colors", colors)
    alpha = hub.projects[0]
    hub.register(make_project_folder(tmp_path / "work" / "a-rather-long-project-name", "host:some/remote/folder/"))
    hub.register(tmp_path / "work" / "gone")
    alpha.changes = 28
    hub.selected = hub.projects.index(alpha)
    rows = [visible(row) for row in hub.table_rows(width)]
    assert len(rows) == 3 and all(len(row) == width for row in rows)
    if width >= 120:
        assert rows[0].startswith("   a-rather-long-project-name") and "SSH" in rows[0]
        assert rows[1].startswith(" › alpha") and "LOCAL" in rows[1] and "28 changes" in rows[1]
        assert "folder not found" in rows[2]


def test_overview_is_alphabetical_and_the_highlight_stays_on_its_project(hub, tmp_path):
    alpha = hub.projects[0]
    for name in ("zeta", "Beta", "00_first", "delta"):
        hub.register(make_project_folder(tmp_path / "work" / name, f"host:projects/{name}/"))
        assert hub.projects[hub.selected] is alpha         # adding others does not move the highlight
    assert [project.name for project in hub.projects] == ["00_first", "alpha", "Beta", "delta", "zeta"]
    assert [root.name for root in config_module.load_registry(hub.registry)] == ["00_first", "alpha", "Beta", "delta", "zeta"]

    # a list saved in another order is sorted when it is read
    config_module.save_registry(hub.registry, [project.root for project in reversed(hub.projects)])
    again = app_module.App(hub.registry, screen=FakeScreen())
    assert [project.name for project in again.projects] == ["00_first", "alpha", "Beta", "delta", "zeta"]


def test_overview_without_projects_says_what_to_do(tmp_path):
    app = app_module.App(tmp_path / "projects.toml", screen=FakeScreen())
    assert "Press a to add" in app.table_rows(120)[0]
    app.on_overview_key("down")                           # nothing to move to; must not fail
    app.on_overview_key("o")
    assert app.view is None


def test_overview_keys_move_open_and_go_back(hub, tmp_path):
    hub.register(make_project_folder(tmp_path / "work" / "beta", "host:projects/beta/"))
    hub.on_overview_key("down")
    hub.on_overview_key("down")                           # stops at the last row
    assert hub.selected == 1
    hub.on_overview_key("up")
    hub.on_overview_key("k")                              # stops at the first row
    assert hub.selected == 0
    hub.on_overview_key("j")
    hub.on_overview_key("\n")
    assert hub.view is hub.projects[1] and not hub.view.touched   # looking is not acting
    hub.on_project_key("esc")
    assert hub.view is None

    hub.on_overview_key("h")
    assert any("highlight" in note for note in hub.notes)
    hub.on_overview_key("v")
    assert not hub.pretty and all(project.verbose for project in hub.projects)
    hub.on_overview_key("x")
    assert [project.name for project in hub.projects] == ["alpha"]


def test_overview_summary_counts_states(hub, tmp_path):
    hub.register(tmp_path / "work" / "gone")
    hub.projects[0].observer = object()                   # as if watching
    hub.projects[0].changes = 4
    hub.summarize()
    assert hub.panel.name == "2 projects"
    assert hub.panel.state == "1 watching, 1 with a problem"
    assert hub.panel.mode == "idle" and hub.panel.level == "problem"
    assert hub.panel.detail == "4 changes"
    hub.projects[0].observer = None


def test_adding_a_folder_that_already_has_a_config(hub, tmp_path, monkeypatch):
    beta = make_project_folder(tmp_path / "work" / "beta", "host:projects/beta/")
    (beta / "deep").mkdir()
    edited = fake_editor(tmp_path, monkeypatch, None)
    answers = iter([str(beta / "deep"), None, str(tmp_path / "no-such-folder")])
    monkeypatch.setattr(hub, "ask", lambda label, text="": next(answers))

    hub.on_overview_key("a")                              # a folder inside the project finds its root
    assert hub.projects[-1].root == beta and hub.selected == 1
    assert not edited.exists()                            # an existing config is not opened for editing
    hub.on_overview_key("a")                              # cancelled
    hub.on_overview_key("a")                              # not a folder
    assert len(hub.projects) == 2 and "Not a folder" in hub.notes[0]


def test_adding_a_folder_without_a_config_creates_one_and_opens_the_editor(hub, tmp_path, monkeypatch):
    folder = (tmp_path / "work" / "fresh").resolve()
    folder.mkdir(parents=True)
    edited = fake_editor(tmp_path, monkeypatch, "host:projects/fresh/")
    monkeypatch.setattr(hub, "ask", lambda label, text="": str(folder))

    hub.on_overview_key("a")
    project = hub.projects[-1]
    assert project.root == folder and hub.selected == 1
    assert edited.read_text().strip() == str(folder / ".xsync.toml")
    assert project.remote == "host:projects/fresh/" and project.error == ""   # read again after editing
    assert "Created" in hub.notes[0]
    assert hub.screen.written.count("<suspend>") == hub.screen.written.count("<resume>") == 1
    config = tomllib.loads((folder / ".xsync.toml").read_text())
    assert ".git/" in config["exclude"]                   # the rest of the starter config is intact


def test_add_prompt_starts_with_the_current_folder(hub, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hub, "ask", lambda label, text="": seen.update(text=text))
    hub.on_overview_key("a")
    assert seen["text"] == str(Path.cwd())


def test_a_missing_editor_is_reported(hub, monkeypatch):
    monkeypatch.setenv("EDITOR", "/no/such/editor")
    monkeypatch.delenv("VISUAL", raising=False)
    hub.on_overview_key("e")
    assert "Could not start the editor" in hub.notes[0]
    assert hub.screen.written.count("<suspend>") == hub.screen.written.count("<resume>") == 1


def test_scrolling_back_through_a_projects_list(hub, capsys):
    project = hub.projects[0]
    hub.activate(project)
    for number in range(100):
        output_module.emit(f"line {number}")
    hub.open(project)
    height = hub._list_height
    assert 0 < height < 100
    assert shown_lines(hub) == [f"line {n}" for n in range(100 - height, 100)]   # the end of the list

    hub.on_project_key("up")
    hub.on_project_key("up")
    assert project.scroll == 2
    assert shown_lines(hub) == [f"line {n}" for n in range(98 - height, 98)]
    assert "scrolled back" in visible(hub.screen.written[-1])

    hub.on_project_key("pageup")
    assert project.scroll == 2 + height - 1
    hub.on_project_key("home")
    assert shown_lines(hub)[0] == "line 0" and project.scroll == 100 - height
    hub.on_project_key("up")                                # already at the start
    assert project.scroll == 100 - height

    capsys.readouterr()
    output_module.emit("line 100")                                  # a new line while scrolled back
    assert capsys.readouterr().out == ""                    # is kept, not printed over what is being read
    assert shown_lines(hub)[0] == "line 0" and project.log[-1].endswith("line 100")

    hub.on_project_key("pagedown")
    assert shown_lines(hub)[0] == f"line {height - 1}"
    hub.on_project_key("end")
    assert project.scroll == 0 and shown_lines(hub)[-1] == "line 100"
    assert "scrolled back" not in visible(hub.screen.written[-1])
    hub.on_project_key("down")                              # already at the end
    assert project.scroll == 0
    output_module.emit("line 101")
    assert "line 101" in capsys.readouterr().out            # following new lines again


def test_scroll_position_resets(hub):
    project = hub.projects[0]
    hub.activate(project)
    for number in range(100):
        output_module.emit(f"line {number}")
    hub.open(project)
    hub.on_project_key("pageup")
    hub.on_project_key("h")                                 # an action jumps to the end to show its output
    assert project.scroll == 0 and "this help" in project.log[-3] + project.log[-2] + project.log[-1]
    hub.on_project_key("pageup")
    hub.on_project_key("b")
    hub.open(project)
    assert project.scroll == 0
    hub.on_project_key("pageup")
    hub.on_project_key("l")
    assert project.scroll == 0 and project.log == deque()
    hub.on_project_key("up")                                # nothing to scroll in an empty list
    assert project.scroll == 0


def test_clearing_a_projects_list(hub):
    project = hub.projects[0]
    hub.open(project)
    hub.activate(project)
    output_module.emit("an old line")
    project.changes = 4
    hub.on_project_key("l")
    assert project.log == deque()
    assert "an old line" not in hub.screen.written[-1] and "\033[2J" in hub.screen.written[-1]
    assert project.changes == 4 and hub.view is project     # only the list is cleared


def test_forgetting_a_project_touches_no_files(hub):
    project = hub.projects[0]
    before = tree(project.root.parent.parent)
    hub.on_overview_key("x")
    assert hub.projects == [] and config_module.load_registry(hub.registry) == []
    assert "no file was touched" in hub.notes[0]
    assert tree(project.root.parent.parent) == before


@needs_gnu_rsync
def test_actions_from_the_overview_apply_to_the_highlighted_project(hub):
    project = hub.projects[0]
    hub.on_overview_key("c")
    assert "3 differences" in project.panel.state and tree(Path(project.remote)) == set()
    hub.on_overview_key("s")
    assert tree(Path(project.remote)) == {"a.txt", "sub/b.txt"}
    assert "alpha" in hub.notes[0]


def test_actions_on_an_unusable_project_only_explain(hub, tmp_path):
    hub.register(tmp_path / "work" / "gone")
    hub.selected = 1
    for key in ("s", "c", "w"):
        hub.on_overview_key(key)
        assert "folder not found" in hub.notes[0]
    hub.open(hub.projects[1])
    hub.on_project_key("s")
    assert "folder not found" in hub.projects[1].log[-1]


def test_a_resize_is_redrawn_from_the_loop_not_the_signal_handler(hub):
    project = hub.projects[0]
    hub.open(project)
    drawn = len(hub.screen.written)
    hub._resized = True                                     # all the signal handler does
    assert len(hub.screen.written) == drawn                 # nothing is drawn at that moment
    hub.after_resize()
    assert len(hub.screen.written) == drawn + 1 and "\033[2J" in hub.screen.written[-1]
    hub.after_resize()                                      # only once per resize
    assert len(hub.screen.written) == drawn + 1
    hub._resized = True
    hub.while_waiting(project, 1.0)                         # also picked up while a sync is running
    assert not hub._resized
