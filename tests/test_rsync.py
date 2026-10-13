"""Running rsync: full syncs, single paths, watch-mode batches and deletes, and reading its output."""

import os
import re
import shutil

import pytest

from xsync import output as output_module, remote as remote_module, rsync as rsync_module
from xsync.output import state as run_state
from helpers import RSYNC, needs_gnu_rsync, needs_rsync, tree


NASTY_NAMES = [
    "with space.txt",
    "it's.txt",
    "$HOME.txt",
    "a; touch pwned.txt",
    "`touch pwned`.txt",
    "-rf",
]


def test_compression_only_over_ssh():
    assert "z" in rsync_module.rsync_flags("host:/srv/project/")
    assert "z" not in rsync_module.rsync_flags("/Volumes/vol/project/")


@pytest.mark.parametrize("output, expected", [
    ("rsync  version 3.2.7  protocol version 31\n", True),
    ("rsync  version 2.6.9  protocol version 29\n", False),
    ("openrsync: protocol version 29\nrsync version 2.6.9 compatible\n", False),
    ("", False),
])
def test_is_gnu_rsync3(output, expected):
    assert rsync_module.is_gnu_rsync3(output) is expected


def test_resolve_rsync_prefers_config():
    assert rsync_module.resolve_rsync({"rsync": "/custom/rsync"}) == "/custom/rsync"


def test_inside_remote_drops_escaping_paths():
    rels = ["a.txt", "sub/b.txt", "../up.txt", "sub/../../up.txt", "/etc/passwd", "", "a\n../up.txt"]
    assert rsync_module.inside_remote(rels) == ["a.txt", "sub/b.txt"]


def test_delete_from_remote_cannot_reach_parent(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    rsync_module.delete_from_remote(["../outside.txt", str(outside)], tmp_path, str(remote) + "/")
    assert outside.exists()


@needs_rsync
def test_sync_batch_cannot_write_to_parent(tmp_path):
    tmp_path = tmp_path.resolve()
    src, remote = tmp_path / "work" / "src", tmp_path / "parent" / "remote"
    src.mkdir(parents=True)
    remote.mkdir(parents=True)
    (src.parent / "up.txt").write_text("x")
    (src / "a.txt").write_text("a")
    rc = rsync_module.sync_batch(["../up.txt", "a.txt"], src, str(remote) + "/", rsync=RSYNC)
    assert rc == 0
    assert tree(tmp_path / "parent") == {"remote/a.txt"}


@needs_rsync
def test_full_sync(project):
    assert project.sync() == 0
    assert tree(project.dst) == {"a.txt", "sub/b.txt"}


@needs_rsync
def test_dry_run_changes_nothing(project):
    before = project.local_files()
    assert project.sync(dry_run=True) == 0
    assert project.sync_path("sub/b.txt", dry_run=True) == 0
    assert project.sync_path("sub", dry_run=True) == 0
    assert tree(project.dst) == set()
    assert project.local_files() == before  # temp files cleaned up


@needs_rsync
def test_deletion_propagates(project):
    assert project.sync() == 0
    (project.src / "a.txt").unlink()
    assert project.sync() == 0
    assert tree(project.dst) == {"sub/b.txt"}


@needs_rsync
def test_single_file_sync_into_missing_nested_folder(project):
    nested = project.src / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "f.py").write_text("print('hi')\n")

    assert project.sync_path("deep/nested/f.py") == 0
    assert tree(project.dst) == {"deep/nested/f.py"}


@needs_rsync
def test_folder_sync_mirrors_only_that_folder(project):
    src, dst = project.src, project.dst
    (src / "sub" / "deeper").mkdir()
    (src / "sub" / "deeper" / "c.txt").write_text("c")
    (src / "sub" / "ignored").mkdir()
    (src / "sub" / "ignored" / "skip.txt").write_text("s")
    (dst / "sub").mkdir()
    (dst / "sub" / "stale.txt").write_text("old")
    (dst / "elsewhere.txt").write_text("keep")

    assert project.sync_path("sub") == 0
    # sub/ is mirrored (stale file gone, excludes respected); nothing outside it is touched
    assert tree(dst) == {"sub/b.txt", "sub/deeper/c.txt", "elsewhere.txt"}


@needs_rsync
def test_syncing_the_project_root_as_a_path_is_a_full_sync(project):
    assert project.sync_path(".") == 0
    assert tree(project.dst) == {"a.txt", "sub/b.txt"}


@needs_rsync
def test_extra_rsync_args_are_passed_through(project):
    assert project.sync(rsync=[*RSYNC, "--exclude=a.txt"]) == 0
    assert tree(project.dst) == {"sub/b.txt"}


@needs_rsync
def test_extra_rsync_args_can_override_archive_flags(project):
    os.utime(project.src / "a.txt", (1_000_000_000, 1_000_000_000))  # 2001
    assert project.sync(rsync=[*RSYNC, "--no-times"]) == 0
    assert (project.dst / "a.txt").stat().st_mtime > 1_500_000_000  # not copied from source


@needs_gnu_rsync
def test_batch_sync_and_delete_handle_awkward_filenames(tmp_path):
    tmp_path = tmp_path.resolve()
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for name in NASTY_NAMES + ["keep.txt"]:
        (src / name).write_text("x")
    remote = str(dst) + "/"

    assert rsync_module.sync_batch(NASTY_NAMES + ["keep.txt"], src, remote, rsync=RSYNC) == 0
    assert tree(dst) == set(NASTY_NAMES) | {"keep.txt"}

    for name in NASTY_NAMES:
        (src / name).unlink()
    assert rsync_module.sync_batch(NASTY_NAMES, src, remote, rsync=RSYNC, delete_missing=True) == 0
    assert tree(dst) == {"keep.txt"}
    assert tree(tmp_path) == {"src/keep.txt", "dst/keep.txt"}  # nothing injected


@needs_gnu_rsync
def test_delete_when_local_parent_folder_is_gone(tmp_path):
    tmp_path = tmp_path.resolve()
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (dst / "gone").mkdir(parents=True)
    (dst / "gone" / "a.txt").write_text("x")
    (dst / "keep.txt").write_text("x")

    rc = rsync_module.sync_batch(["gone/a.txt"], src, str(dst) + "/", rsync=RSYNC, delete_missing=True)
    assert rc == 0
    assert tree(dst) == {"keep.txt"}


def test_delete_from_local_remote(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / "keep.txt").write_text("k")
    rc = rsync_module.delete_from_remote(["sub/b.txt", "missing.txt"], tmp_path, str(tmp_path) + "/")
    assert rc == 0
    assert tree(tmp_path) == {"keep.txt"}


def test_missing_parent_dirs(tmp_path):
    (tmp_path / "kept").mkdir()
    rels = ["gone/deep/a.txt", "gone/b.txt", "kept/c.txt", "top.txt"]
    assert rsync_module.missing_parent_dirs(rels, tmp_path) == [["gone/deep"], ["gone"]]


def test_local_delete_removes_emptied_folders_only(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for rel in ("gone/deep/a.txt", "shared/b.txt", "shared/remote_only.txt"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text("x")

    assert rsync_module.delete_from_remote(["gone/deep/a.txt", "shared/b.txt"], src, str(dst) + "/") == 0
    assert not (dst / "gone").exists()
    assert tree(dst) == {"shared/remote_only.txt"}


@needs_gnu_rsync
def test_rsync_removes_emptied_folders_only(tmp_path):
    """The per-depth passes delete_from_remote runs over SSH, exercised against a local folder."""
    tmp_path = tmp_path.resolve()
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for rel in ("gone/deep/a.txt", "shared/b.txt", "shared/remote_only.txt"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text("x")
    remote = str(dst) + "/"
    rels = ["gone/deep/a.txt", "shared/b.txt"]

    assert rsync_module.sync_batch(rels, src, remote, rsync=RSYNC, delete_missing=True) == 0
    for level in rsync_module.missing_parent_dirs(rels, src):
        rsync_module.sync_batch(level, src, remote, rsync=RSYNC, delete_missing=True, quiet=True)

    assert not (dst / "gone").exists()
    assert tree(dst) == {"shared/remote_only.txt"}


@pytest.mark.parametrize("line, expected", [
    ("<f+++++++++ src/new.py", ("new", "src/new.py")),
    ("cd+++++++++ src/pkg/", ("new", "src/pkg/")),
    ("<f.st...... README.md", ("updated", "README.md")),
    ("<fc........ same size.txt", ("updated", "same size.txt")),
    ("*deleting   old/file.py", ("deleted", "old/file.py")),
    (".d..t...... ./", ("skip", "./")),
    (".f...p..... a.txt", ("skip", "a.txt")),
    ("", ("skip", "")),
    ("rsync: [generator] failed to set times on \"/x\": Operation not permitted (1)",
     ("message", "rsync: [generator] failed to set times on \"/x\": Operation not permitted (1)")),
    ("cannot delete non-empty directory: pkg/src", ("message", "cannot delete non-empty directory: pkg/src")),
])
def test_parse_change(line, expected):
    assert rsync_module.parse_change(line) == expected


@needs_gnu_rsync
def test_summarized_output_and_counters(project, monkeypatch, capsys):
    monkeypatch.setattr(run_state, "pretty", True)
    monkeypatch.setattr(run_state, "changes", 0)

    # compare: lists differences, counts them, changes nothing
    assert project.sync(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "+ a.txt  (not synced)" in out
    assert run_state.last_counts == {"new": 3, "updated": 0, "deleted": 0}  # a.txt, sub/, sub/b.txt
    assert run_state.changes == 0
    assert tree(project.dst) == set()

    # sync: one line per change, counted
    assert project.sync() == 0
    out = capsys.readouterr().out
    assert re.search(r"^\d\d:\d\d:\d\d  \+ sub/b\.txt$", out, re.MULTILINE)
    assert "not synced" not in out
    assert run_state.changes == 3

    # nothing to do: no lines
    assert project.sync() == 0
    assert capsys.readouterr().out == ""
    assert sum(run_state.last_counts.values()) == 0

    # an edit and a delete
    (project.src / "a.txt").write_text("changed content")
    (project.src / "sub" / "b.txt").unlink()
    assert project.sync() == 0
    out = capsys.readouterr().out
    assert "~ a.txt" in out and "- sub/b.txt" in out
    assert run_state.last_counts == {"new": 0, "updated": 1, "deleted": 1}
    assert "sending incremental file list" not in out


@needs_gnu_rsync
def test_warnings_reach_the_problem_hook(project, monkeypatch, capsys):
    seen = []

    class Recorder(output_module.Printer):
        def problem(self, text):
            seen.append(text)

    monkeypatch.setattr(run_state, "pretty", True)
    monkeypatch.setattr(run_state, "output", Recorder())
    rc = project.sync(remote=str(project.dst / "missing" / "deeper") + "/")  # parent folder does not exist
    assert rc != 0
    assert seen and any("rsync" in text for text in seen)
    assert "! " in capsys.readouterr().out


@needs_gnu_rsync
def test_ssh_sync_to_localhost(project, ssh_localhost):
    src, dst = project.src, project.dst
    remote = f"localhost:{dst}/"
    rsync = [*RSYNC, "-e", remote_module.SSH_COMMAND]

    assert rsync_module.ssh_reachable(remote, rsync)
    assert rsync_module.sync_full(src, remote, project.exclude_file, False, rsync) == 0
    assert tree(dst) == {"a.txt", "sub/b.txt"}

    nested = src / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "it's a file.py").write_text("x")
    assert rsync_module.sync_path("deep/nested/it's a file.py", src, remote, project.exclude_file, False, rsync) == 0
    assert "deep/nested/it's a file.py" in tree(dst)

    shutil.rmtree(src / "deep")
    (src / "a.txt").unlink()
    rels = ["deep/nested/it's a file.py", "a.txt"]
    assert rsync_module.delete_from_remote(rels, src, remote, rsync) == 0
    assert tree(dst) == {"sub/b.txt"}
    assert not (dst / "deep").exists()  # emptied folders are removed too
