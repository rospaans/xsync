"""One project: loading its config, compare and sync, watch mode with the real file watcher."""

from collections import deque
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

import xsync
from xsync import config as config_module, project as project_module, remote as remote_module
from helpers import EXCLUDES, REPO, RSYNC, fake_editor, make_project_folder, needs_gnu_rsync, needs_rsync, run_ticks, tree


@pytest.mark.parametrize("config, expected", [
    (None, "no .xsync.toml"),
    ('exclude = []\n', "'remote' is not set"),
    (config_module.STARTER_CONFIG, "set 'remote' in .xsync.toml first"),
    ('remote = "/"\n', "refusing to sync"),
    ('remote = "host:project/../../"\n', "refusing to sync"),
    ('remote = "host:project/"\ninterval = "soon"\n', "'interval'"),
    ('remote = "host:project/"\nrsync = "/no/such/rsync"\n', "rsync not found"),
    ('remote = [broken', "invalid .xsync.toml"),
])
def test_unusable_projects_say_why(hub, tmp_path, config, expected):
    folder = tmp_path / "work" / "broken"
    folder.mkdir(parents=True)
    if config is not None:
        (folder / ".xsync.toml").write_text(config)
    project = hub.register(folder)
    assert expected in project.error
    assert project.panel.level == "problem" and expected in project.panel.state
    project.tick(1e12)                                    # never tries to sync or probe
    assert project.log == deque()


def test_missing_project_folder_is_reported(hub, tmp_path):
    assert hub.register(tmp_path / "work" / "gone").error == "folder not found"


def test_project_settings_come_from_its_config(hub, tmp_path):
    folder = make_project_folder(tmp_path / "work" / "beta", "myhost:projects/beta",
                                 'interval = 5\nrsync_args = ["--inplace"]\n')
    project = hub.register(folder)
    assert project.error == ""
    assert project.remote == "myhost:projects/beta/"     # trailing slash added
    assert project.interval == 5
    assert project.panel.transport == "ssh"
    assert project.rsync[1:] == ["-e", remote_module.SSH_COMMAND, "--inplace"]   # the user's flags come last


@needs_gnu_rsync
def test_project_compare_then_sync(hub):
    project = hub.projects[0]
    dst = Path(project.remote)

    project.compare()
    assert project.resting == (project.panel.state, "busy") and "3 differences" in project.panel.state
    assert tree(dst) == set() and project.changes == 0
    assert any("+ a.txt" in line and "not synced" in line for line in project.log)

    project.full_sync()
    assert tree(dst) == {"a.txt", "sub/b.txt"}
    assert project.changes == 3 and project.last_change
    assert project.panel.state.startswith("in sync at") and project.panel.level == "ok"
    assert project.panel.detail.startswith("3 changes · last ")

    project.full_sync()
    assert "already up to date" in project.log[-1]
    assert project.changes == 3
    assert tree(project.root) == {".xsync.toml", "a.txt", "sub/b.txt"}   # no temp files left behind


@needs_gnu_rsync
def test_project_sync_one_path(hub):
    project = hub.projects[0]
    project.sync_one("sub/b.txt")
    assert tree(Path(project.remote)) == {"sub/b.txt"}
    project.sync_one("nowhere.txt")
    assert "not found inside the project" in project.log[-1]
    assert project.problems and "not found" in project.panel.message


@needs_gnu_rsync
def test_project_flush_sends_and_deletes_queued_changes(hub):
    project = hub.projects[0]
    dst = Path(project.remote)
    project.full_sync()

    (project.root / "new.txt").write_text("n")
    (project.root / "sub" / "b.txt").unlink()
    project.pending.update({"new.txt", "temp-file-that-vanished"})
    project.deleted.add("sub/b.txt")
    project.flush()

    assert tree(dst) == {"a.txt", "new.txt"}
    assert project.pending == set() and project.deleted == set()
    assert project.changes == 5                           # 3 from the full sync, then + new.txt and - sub/b.txt
    assert project.panel.state.startswith("in sync at")   # not watching, so back to the resting state


@needs_gnu_rsync
def test_each_project_keeps_its_own_list_and_counter(hub, tmp_path):
    alpha = hub.projects[0]
    beta_remote = tmp_path / "remote" / "beta"
    beta_remote.mkdir(parents=True)
    beta = hub.register(make_project_folder(tmp_path / "work" / "beta", str(beta_remote) + "/"))
    (beta.root / "only-in-beta.txt").write_text("b")

    alpha.full_sync()
    beta.full_sync()
    assert alpha.changes == 3 and beta.changes == 1
    assert any("only-in-beta.txt" in line for line in beta.log)
    assert not any("only-in-beta.txt" in line for line in alpha.log)
    assert tree(beta_remote) == {"only-in-beta.txt"}
    assert tree(Path(alpha.remote)) == {"a.txt", "sub/b.txt"}


def test_watch_sends_edits_and_deletes(watched):
    project, dst = watched
    assert tree(dst) == {"a.txt", "sub/b.txt"}            # switching watch on starts with a full sync
    time.sleep(0.5)                                         # let the watcher settle
    (project.root / "new").mkdir()
    (project.root / "new" / "n.txt").write_text("n")
    (project.root / "a.txt").write_text("changed")
    run_ticks(project, 2)
    assert tree(dst) == {"a.txt", "sub/b.txt", "new/n.txt"}
    assert (dst / "a.txt").read_text() == "changed"

    shutil.rmtree(project.root / "new")
    (project.root / "sub" / "b.txt").unlink()
    run_ticks(project, 2)
    assert tree(dst) == {"a.txt"}
    assert not (dst / "new").exists()                      # the emptied folder is gone too
    assert not any("! " in line for line in project.log)


def test_a_burst_leaves_the_remote_identical(watched):
    """Thousands of changes at once: whatever the watcher misses is caught by the check afterwards."""
    project, dst = watched
    time.sleep(0.5)
    burst = subprocess.Popen([sys.executable, str(REPO / "tests" / "stress_edits.py"),
                              "--files", "3000", "--pause", "0", "--keep"], cwd=project.root, stdout=subprocess.DEVNULL)
    run_ticks(project, 12)
    assert burst.wait() == 0
    assert tree(dst) == tree(project.root) - {".xsync.toml"}
    assert len(tree(dst)) > 500

    shutil.rmtree(project.root / "xsync_stress_test")
    run_ticks(project, 8)
    assert tree(dst) == {"a.txt", "sub/b.txt"}
    assert not (dst / "xsync_stress_test").exists()


@needs_gnu_rsync
def test_check_after_burst_mirrors_only_the_touched_folders(hub):
    project = hub.projects[0]
    dst = Path(project.remote)
    project.full_sync()
    (project.root / "sub" / "deep").mkdir()
    (project.root / "sub" / "deep" / "d.txt").write_text("d")
    project.full_sync()
    # Drift the watcher never reported: a stale file, a missing file, a stale folder
    (dst / "sub" / "stale.txt").write_text("should go")
    (dst / "sub" / "deep" / "untouched-stale.txt").write_text("in a folder that was not touched")
    (dst / "gone").mkdir()
    (dst / "gone" / "old.txt").write_text("its local folder was deleted")
    (dst / "sub" / "ignored").mkdir()
    (dst / "sub" / "ignored" / "remote-only.txt").write_text("excluded: never ours to delete")
    (project.root / "sub" / "missed.txt").write_text("never reported")

    project.observer = object()                              # as if watching
    project.touched_dirs = {"sub", "gone"}
    project.unchecked = project_module.BURST_PATHS
    project.last_event = time.monotonic() - 60
    project.tick(time.monotonic())
    project.observer = None

    assert tree(dst) == {"a.txt", "sub/b.txt", "sub/missed.txt", "sub/deep/d.txt",
                         "sub/deep/untouched-stale.txt", "sub/ignored/remote-only.txt"}
    assert project.touched_dirs == set() and project.unchecked == 0
    assert "after a burst" in project.log[-1]


def test_a_few_changes_do_not_trigger_the_check(hub, monkeypatch):
    project = hub.projects[0]
    monkeypatch.setattr(project, "check_after_burst", lambda: pytest.fail("checked without a burst"))
    project.observer = object()
    project.touched_dirs, project.unchecked = {"sub"}, 5
    project.last_event = time.monotonic() - 10
    project.tick(time.monotonic())
    assert project.touched_dirs == {"sub"}                 # kept for now
    project.last_event = time.monotonic() - 60
    project.tick(time.monotonic())
    assert project.touched_dirs == set() and project.unchecked == 0   # forgotten once long settled
    project.observer = None


@needs_gnu_rsync
def test_cancelling_a_sync(hub, monkeypatch):
    project = hub.projects[0]
    for number in range(300):
        (project.root / f"file_{number}.txt").write_text("x" * 2000)
    monkeypatch.setattr(hub, "while_waiting", lambda project, seconds: True)   # as if Esc was pressed at once
    slow = [*project.rsync, "--bwlimit=20"]                 # slow enough to still be running
    monkeypatch.setattr(project, "rsync", slow)
    project.full_sync()
    assert "cancelled" in project.panel.state and any("cancelled" in line for line in project.log)
    assert len(tree(Path(project.remote))) < 300
    assert not any("rsync exited" in line for line in project.log)


def test_starting_and_opening_run_nothing(hub, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("rsync or ssh was started without a key press")

    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    project = hub.projects[0]
    hub.draw()
    hub.open(project)
    for _ in range(3):
        project.tick(1e12)                                 # long after any timer would be due
    hub.back()
    assert project.log == deque() and project.panel.state == "idle, nothing checked yet"
    assert not project.touched and not project.watching


def test_a_new_config_left_unedited_is_not_usable(hub, tmp_path, monkeypatch):
    folder = (tmp_path / "work" / "fresh").resolve()
    folder.mkdir(parents=True)
    fake_editor(tmp_path, monkeypatch, None)
    monkeypatch.setattr(hub, "ask", lambda label, text="": str(folder))
    hub.on_overview_key("a")
    project = hub.projects[-1]
    assert "set 'remote'" in project.error
    hub.on_overview_key("s")                              # refuses instead of contacting "myhost"
    assert "set 'remote'" in hub.notes[0] and project.log[-1].endswith(project.error)


@pytest.mark.parametrize("from_view", ["overview", "project"])
def test_editing_a_config_reloads_the_project(hub, tmp_path, monkeypatch, from_view):
    project = hub.projects[0]
    edited = fake_editor(tmp_path, monkeypatch, "host:somewhere/else/")
    project.observer = None
    if from_view == "project":
        hub.open(project)
        hub.on_project_key("e")
        assert hub.view is project                        # still looking at the project afterwards
    else:
        hub.on_overview_key("e")
    assert edited.read_text().strip() == str(project.root / ".xsync.toml")
    assert project.remote == "host:somewhere/else/" and project.panel.remote == project.remote
    assert project.panel.transport == "ssh"
    assert any("config reloaded" in line for line in project.log)


def test_editing_into_a_broken_config_shows_the_reason(hub, tmp_path, monkeypatch):
    project = hub.projects[0]
    fake_editor(tmp_path, monkeypatch, "/")
    hub.on_overview_key("e")
    assert "refusing to sync" in project.error and "refusing to sync" in hub.notes[0]
    fake_editor(tmp_path, monkeypatch, str(tmp_path / "remote" / "alpha") + "/")
    hub.on_overview_key("e")                              # and fixing it makes the project usable again
    assert project.error == "" and project.panel.level != "problem"


@needs_rsync
def test_status_rows(project):
    version = subprocess.run([RSYNC[0], "--version"], capture_output=True, text=True).stdout
    config = {"remote": project.remote, "exclude": EXCLUDES, "rsync_args": ["--inplace"], "interval": 5}
    rows, ok = project_module.status_rows(project.src, config, project.remote, RSYNC, version)
    info = dict(rows)
    assert ok
    assert info["version"] == f"xsync {xsync.__version__}"
    assert info["project"] == str(project.src)
    assert project.remote in info["remote"] and "local path" in info["remote"]
    assert info["target"] == "folder exists"
    assert info["full sync"] == "allowed"
    assert info["rsync_args"] == "--inplace"
    assert info["exclude"] == f"{len(EXCLUDES)} patterns"
    assert info["interval"].startswith("5s")
    assert tree(project.dst) == set()  # looking does not sync

    rows, ok = project_module.status_rows(project.src, config, "/", RSYNC, version)
    assert not ok
    assert dict(rows)["full sync"].startswith("refused")
