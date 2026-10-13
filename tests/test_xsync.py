"""Tests for xsync. Run with: python -m pytest tests

xsync itself is interactive and needs a terminal, so these tests call its
functions directly. Only --help, --version, --init and argument checking are
run through the command line.
"""

import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

XSYNC_PATH = Path(__file__).resolve().parent.parent / "xsync"


def load_xsync():
    """Import the extensionless xsync script as a module."""
    loader = importlib.machinery.SourceFileLoader("xsync", str(XSYNC_PATH))
    spec = importlib.util.spec_from_loader("xsync", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


xsync = load_xsync()
HOME = str(Path.home())


# --- helpers ------------------------------------------------------------------

EXCLUDES = [".xsync.toml", ".xsync.exclude", ".xsync.files", "ignored/"]
RSYNC = [xsync.resolve_rsync({})]

needs_rsync = pytest.mark.skipif(shutil.which(RSYNC[0]) is None, reason="rsync not available")


def _default_rsync_is_gnu() -> bool:
    try:
        out = subprocess.run([RSYNC[0], "--version"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return xsync.is_gnu_rsync3(out)


needs_gnu_rsync = pytest.mark.skipif(not _default_rsync_is_gnu(), reason="needs GNU rsync 3.x")


def run_xsync(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the script without a terminal."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, str(XSYNC_PATH), *args],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
    )


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def visible(text: str) -> str:
    """Strip terminal color codes."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


class Project:
    """A source project with a few files, and an empty local destination."""

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
        self.exclude_file = xsync.write_exclude_file(self.src, EXCLUDES)

    def sync(self, dry_run: bool = False, rsync: list[str] = RSYNC, remote: str | None = None) -> int:
        return xsync.sync_full(self.src, remote or self.remote, self.exclude_file, dry_run, rsync)

    def sync_path(self, rel: str, dry_run: bool = False) -> int:
        return xsync.sync_path(rel, self.src, self.remote, self.exclude_file, dry_run, RSYNC)

    def local_files(self) -> set[str]:
        return tree(self.src) - {".xsync.exclude"}


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


# --- is_ssh_remote ---------------------------------------------------------

@pytest.mark.parametrize("remote, expected", [
    ("/Volumes/vol/project/", False),
    ("~/project/", False),
    ("./project/", False),
    ("host:/path", True),
    ("user@host:/path", True),
])
def test_is_ssh_remote(remote, expected):
    assert xsync.is_ssh_remote(remote) is expected


# --- make_excluded_checker -------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("/repo/main.py", False),
    ("/repo/src/main.py", False),
    # file patterns
    ("/repo/main.pyc", True),
    ("/repo/src/main.pyc", True),
    ("/repo/.DS_Store", True),
    # directory patterns
    ("/repo/.git/config", True),
    ("/repo/data/table.csv", True),
    # nested directories
    ("/repo/src/data/deep/table.csv", True),
    ("/repo/src/pkg/__pycache__/x.py", True),
    # internal files
    ("/repo/.xsync.files", True),
    ("/repo/.xsync.exclude", True),
    # outside the repo
    ("/elsewhere/main.py", True),
])
def test_make_excluded_checker(path, expected):
    patterns = [".git/", "data/", "__pycache__/", "*.pyc", ".DS_Store"]
    is_excluded = xsync.make_excluded_checker(Path("/repo"), patterns)
    assert is_excluded(path) is expected


@pytest.mark.parametrize("path, expected", [
    # "/data/" is anchored: only the top-level data folder
    ("/repo/data/table.csv", True),
    ("/repo/data/raw/deep/table.csv", True),
    ("/repo/src/pkg/data/loader.py", False),
    ("/repo/data.py", False),
    # multi-part directory pattern
    ("/repo/scripts/experiments/soma/README.md", True),
    ("/repo/scripts/experiments/soma/src/model.py", True),
    ("/repo/scripts/experiments/other/run.py", False),
    ("/repo/scripts/soma.py", False),
    # anchored file pattern
    ("/repo/notes.txt", True),
    ("/repo/docs/notes.txt", False),
    # a directory pattern does not match a file of the same name
    ("/repo/src/outputs", False),
    ("/repo/src/outputs/run1.json", True),
])
def test_excluded_checker_anchored_and_nested_patterns(path, expected):
    patterns = ["/data/", "scripts/experiments/soma/", "/notes.txt", "outputs/"]
    is_excluded = xsync.make_excluded_checker(Path("/repo"), patterns)
    assert is_excluded(path) is expected


# --- --delete guard --------------------------------------------------------

@pytest.mark.parametrize("remote", [
    "",
    "/",
    "~",
    "~/",
    ".",
    "./",
    HOME,
    HOME + "/",
    "host:",
    "host:/",
    "host:~",
    "host:~/",
    "host:.",
    "user@host:/",
    f"host:{HOME}/",
    "host:/srv/project/../",
    "host:project/../../other/",
    "/Volumes/vol/project/../other/",
])
def test_dangerous_remotes_are_rejected(remote):
    assert xsync.unsafe_remote_reason(remote) is not None


@pytest.mark.parametrize("remote", [
    "host:/srv/project/",
    "host:project/",
    "user@host:~/project/",
    "/Volumes/vol/project/",
    "~/project/",
    HOME + "/project",
])
def test_normal_remotes_pass(remote):
    assert xsync.unsafe_remote_reason(remote) is None


def test_normalize_remote():
    assert xsync.normalize_remote("host:/srv/project") == "host:/srv/project/"
    assert xsync.normalize_remote("host:~/project/") == "host:~/project/"
    assert xsync.normalize_remote("/Volumes/vol/project") == "/Volumes/vol/project/"
    assert xsync.normalize_remote("~/project") == HOME + "/project/"


# --- rsync flags and flavor ------------------------------------------------

def test_compression_only_over_ssh():
    assert "z" in xsync.rsync_flags("host:/srv/project/")
    assert "z" not in xsync.rsync_flags("/Volumes/vol/project/")


@pytest.mark.parametrize("output, expected", [
    ("rsync  version 3.2.7  protocol version 31\n", True),
    ("rsync  version 2.6.9  protocol version 29\n", False),
    ("openrsync: protocol version 29\nrsync version 2.6.9 compatible\n", False),
    ("", False),
])
def test_is_gnu_rsync3(output, expected):
    assert xsync.is_gnu_rsync3(output) is expected


def test_resolve_rsync_prefers_config():
    assert xsync.resolve_rsync({"rsync": "/custom/rsync"}) == "/custom/rsync"


# --- paths stay inside the remote project ----------------------------------

def test_inside_remote_drops_escaping_paths():
    rels = ["a.txt", "sub/b.txt", "../up.txt", "sub/../../up.txt", "/etc/passwd", "", "a\n../up.txt"]
    assert xsync.inside_remote(rels) == ["a.txt", "sub/b.txt"]


def test_delete_from_remote_cannot_reach_parent(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    xsync.delete_from_remote(["../outside.txt", str(outside)], tmp_path, str(remote) + "/")
    assert outside.exists()


@needs_rsync
def test_sync_batch_cannot_write_to_parent(tmp_path):
    tmp_path = tmp_path.resolve()
    src, remote = tmp_path / "work" / "src", tmp_path / "parent" / "remote"
    src.mkdir(parents=True)
    remote.mkdir(parents=True)
    (src.parent / "up.txt").write_text("x")
    (src / "a.txt").write_text("a")
    rc = xsync.sync_batch(["../up.txt", "a.txt"], src, str(remote) + "/", rsync=RSYNC)
    assert rc == 0
    assert tree(tmp_path / "parent") == {"remote/a.txt"}


def test_resolve_in_project(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "proj"
    (root / "sub dir").mkdir(parents=True)
    (root / "sub dir" / "a file.py").write_text("x")
    (root / "top.py").write_text("x")
    outside = tmp_path.resolve() / "outside.txt"
    outside.write_text("x")
    monkeypatch.chdir(root / "sub dir")

    assert xsync.resolve_in_project("top.py", root) == "top.py"                      # relative to the root
    assert xsync.resolve_in_project("a file.py", root) == "sub dir/a file.py"        # relative to the cwd
    assert xsync.resolve_in_project(str(root / "top.py"), root) == "top.py"          # absolute
    assert xsync.resolve_in_project(f"'{root}/sub dir/a file.py'  ", root) == "sub dir/a file.py"  # quoted paste
    dragged = str(root / "sub dir" / "a file.py").replace(" ", "\\ ")
    assert xsync.resolve_in_project(dragged, root) == "sub dir/a file.py"            # dragged from Finder
    assert xsync.resolve_in_project("sub dir", root) == "sub dir"                    # a folder
    assert xsync.resolve_in_project(str(root), root) == "."
    assert xsync.resolve_in_project("missing.py", root) is None
    assert xsync.resolve_in_project(str(outside), root) is None
    assert xsync.resolve_in_project("../../outside.txt", root) is None
    assert xsync.resolve_in_project("   ", root) is None


# --- syncing: full, one path, dry run, deletes -----------------------------

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


# --- watch-mode deletes ------------------------------------------------------

NASTY_NAMES = [
    "with space.txt",
    "it's.txt",
    "$HOME.txt",
    "a; touch pwned.txt",
    "`touch pwned`.txt",
    "-rf",
]


@needs_gnu_rsync
def test_batch_sync_and_delete_handle_awkward_filenames(tmp_path):
    tmp_path = tmp_path.resolve()
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for name in NASTY_NAMES + ["keep.txt"]:
        (src / name).write_text("x")
    remote = str(dst) + "/"

    assert xsync.sync_batch(NASTY_NAMES + ["keep.txt"], src, remote, rsync=RSYNC) == 0
    assert tree(dst) == set(NASTY_NAMES) | {"keep.txt"}

    for name in NASTY_NAMES:
        (src / name).unlink()
    assert xsync.sync_batch(NASTY_NAMES, src, remote, rsync=RSYNC, delete_missing=True) == 0
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

    rc = xsync.sync_batch(["gone/a.txt"], src, str(dst) + "/", rsync=RSYNC, delete_missing=True)
    assert rc == 0
    assert tree(dst) == {"keep.txt"}


def test_delete_from_local_remote(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / "keep.txt").write_text("k")
    rc = xsync.delete_from_remote(["sub/b.txt", "missing.txt"], tmp_path, str(tmp_path) + "/")
    assert rc == 0
    assert tree(tmp_path) == {"keep.txt"}


def test_missing_parent_dirs(tmp_path):
    (tmp_path / "kept").mkdir()
    rels = ["gone/deep/a.txt", "gone/b.txt", "kept/c.txt", "top.txt"]
    assert xsync.missing_parent_dirs(rels, tmp_path) == [["gone/deep"], ["gone"]]


def test_local_delete_removes_emptied_folders_only(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for rel in ("gone/deep/a.txt", "shared/b.txt", "shared/remote_only.txt"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text("x")

    assert xsync.delete_from_remote(["gone/deep/a.txt", "shared/b.txt"], src, str(dst) + "/") == 0
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

    assert xsync.sync_batch(rels, src, remote, rsync=RSYNC, delete_missing=True) == 0
    for level in xsync.missing_parent_dirs(rels, src):
        xsync.sync_batch(level, src, remote, rsync=RSYNC, delete_missing=True, quiet=True)

    assert not (dst / "gone").exists()
    assert tree(dst) == {"shared/remote_only.txt"}


# --- summarized output -------------------------------------------------------

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
    assert xsync.parse_change(line) == expected


@needs_gnu_rsync
def test_summarized_output_and_counters(project, monkeypatch, capsys):
    monkeypatch.setattr(xsync, "PRETTY", True)
    monkeypatch.setattr(xsync, "CHANGES", 0)

    # compare: lists differences, counts them, changes nothing
    assert project.sync(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "+ a.txt  (not synced)" in out
    assert xsync.LAST_COUNTS == {"new": 3, "updated": 0, "deleted": 0}  # a.txt, sub/, sub/b.txt
    assert xsync.CHANGES == 0
    assert tree(project.dst) == set()

    # sync: one line per change, counted
    assert project.sync() == 0
    out = capsys.readouterr().out
    assert re.search(r"^\d\d:\d\d:\d\d  \+ sub/b\.txt$", out, re.MULTILINE)
    assert "not synced" not in out
    assert xsync.CHANGES == 3

    # nothing to do: no lines
    assert project.sync() == 0
    assert capsys.readouterr().out == ""
    assert sum(xsync.LAST_COUNTS.values()) == 0

    # an edit and a delete
    (project.src / "a.txt").write_text("changed content")
    (project.src / "sub" / "b.txt").unlink()
    assert project.sync() == 0
    out = capsys.readouterr().out
    assert "~ a.txt" in out and "- sub/b.txt" in out
    assert xsync.LAST_COUNTS == {"new": 0, "updated": 1, "deleted": 1}
    assert "sending incremental file list" not in out


@needs_gnu_rsync
def test_warnings_reach_the_problem_hook(project, monkeypatch, capsys):
    seen = []

    class Recorder(xsync.Printer):
        def problem(self, text):
            seen.append(text)

    monkeypatch.setattr(xsync, "PRETTY", True)
    monkeypatch.setattr(xsync, "OUTPUT", Recorder())
    rc = project.sync(remote=str(project.dst / "missing" / "deeper") + "/")  # parent folder does not exist
    assert rc != 0
    assert seen and any("rsync" in text for text in seen)
    assert "! " in capsys.readouterr().out


# --- the fixed rows of a view ----------------------------------------------------

def make_panel():
    panel = xsync.Panel("a_project", xsync.PROJECT_KEYS, "host:some/long/remote/folder/", "ssh")
    panel.switches = {"w": False, "v": False}
    return panel


@pytest.mark.parametrize("colors", [False, True])
@pytest.mark.parametrize("width", [1, 12, 25, 40, 80, 120, 200])
def test_fixed_rows_never_exceed_terminal_width(width, colors, monkeypatch):
    monkeypatch.setattr(xsync, "colors_on", lambda: colors)
    panel = make_panel()
    states = [("watch", xsync.IDLE, "ok", "WATCH"), ("idle", "3 differences at 15:44", "busy", "XSYNC"),
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
    monkeypatch.setattr(xsync, "colors_on", lambda: True)
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
    panel = xsync.Panel("3 projects", xsync.OVERVIEW_KEYS)
    panel.state = "2 watching"
    for width in (10, 40, 120):
        bar = panel.bar(width)
        assert len(bar) <= width
    assert "3 projects" in panel.bar(120) and "→" not in panel.bar(120)


@pytest.mark.parametrize("keys, help_text", [
    (xsync.PROJECT_KEYS, xsync.KEY_HELP), (xsync.OVERVIEW_KEYS, xsync.OVERVIEW_HELP),
])
def test_every_button_has_a_unique_key_and_help(keys, help_text):
    flat = [key for row in keys for key, _ in row]
    assert len(flat) == len(set(flat))
    assert set(flat) <= set(help_text)
    rows = "\n".join(xsync.Panel("p", keys).key_rows(300))
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
    monkeypatch.setattr(xsync, "colors_on", lambda: colors)
    panel = make_panel()
    count = sum(len(row) for row in xsync.PROJECT_KEYS)

    def layout(width):
        return [visible(row) for row in panel.key_rows(width)]

    assert len(layout(200)) == 2                       # all columns
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


# --- the hub: remembered projects, the overview, a project's state -------------------

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


def make_project_folder(root: Path, remote: str, extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    excludes = ", ".join(f'"{pattern}"' for pattern in EXCLUDES)
    (root / ".xsync.toml").write_text(f'remote = "{remote}"\nexclude = [{excludes}]\n{extra}')
    return root.resolve()


@pytest.fixture
def hub(tmp_path):
    """An app with one project (src → dst, a few files) and a fake terminal."""
    tmp_path = tmp_path.resolve()
    src = make_project_folder(tmp_path / "work" / "alpha", str(tmp_path / "remote" / "alpha") + "/")
    (tmp_path / "remote" / "alpha").mkdir(parents=True)
    (src / "sub").mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    app = xsync.App(tmp_path / "config" / "projects.toml", screen=FakeScreen())
    app.register(src)
    yield app
    for project in app.projects:
        project.close()
    xsync.OUTPUT = xsync.Printer()
    xsync.PRETTY = False


def test_registry_round_trip(tmp_path):
    path = tmp_path / "config" / "xsync" / "projects.toml"
    assert xsync.load_registry(path) == []
    roots = [tmp_path / "one", tmp_path / 'with "quotes" and spaces']
    xsync.save_registry(path, roots)
    assert xsync.load_registry(path) == roots
    assert tomllib.loads(path.read_text())["projects"] == [str(root) for root in roots]
    path.write_text("not [valid toml")
    assert xsync.load_registry(path) == []


def test_registry_lives_in_the_user_config_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert xsync.registry_path() == tmp_path / "xsync" / "projects.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert xsync.registry_path() == Path.home() / ".config" / "xsync" / "projects.toml"


def test_projects_are_remembered_once_and_can_be_removed(hub, tmp_path):
    alpha = hub.projects[0]
    assert hub.register(alpha.root) is alpha            # already known
    beta = hub.register(make_project_folder(tmp_path / "work" / "beta", "host:projects/beta/"))
    assert [project.name for project in hub.projects] == ["alpha", "beta"]

    again = xsync.App(hub.registry, screen=FakeScreen())  # a new run sees the same list
    assert [project.root for project in again.projects] == [alpha.root, beta.root]

    hub.remove(beta)
    assert xsync.load_registry(hub.registry) == [alpha.root]
    assert (beta.root / ".xsync.toml").exists()          # only the list entry is gone


@pytest.mark.parametrize("config, expected", [
    (None, "no .xsync.toml"),
    ('exclude = []\n', "'remote' is not set"),
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
    assert project.log == xsync.deque()


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
    assert project.rsync[1:] == ["-e", xsync.SSH_COMMAND, "--inplace"]   # the user's flags come last


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


def test_only_the_project_on_screen_prints_its_lines(hub, capsys):
    project = hub.projects[0]
    hub.activate(project)
    xsync.emit("hidden line")
    assert capsys.readouterr().out == ""                  # the overview is showing
    hub.open(project)
    xsync.emit("visible line")
    assert "visible line" in capsys.readouterr().out
    assert [line.split("  ", 1)[1] for line in project.log] == ["hidden line", "visible line"]


def test_opening_a_project_replays_the_end_of_its_list(hub):
    project = hub.projects[0]
    hub.activate(project)
    for number in range(100):
        xsync.emit(f"line {number}")
    hub.open(project)
    drawn = hub.screen.written[-1]
    assert "line 99" in drawn and "line 0\n" not in drawn  # only what fits below the fixed rows
    hub.back()
    assert hub.view is None and "\033[r" in hub.screen.written[-1]


@pytest.mark.parametrize("colors", [False, True])
@pytest.mark.parametrize("width", [1, 20, 60, 120, 200])
def test_overview_rows_fit_and_mark_the_highlighted_project(hub, tmp_path, monkeypatch, width, colors):
    monkeypatch.setattr(xsync, "colors_on", lambda: colors)
    hub.register(make_project_folder(tmp_path / "work" / "a-rather-long-project-name", "host:some/remote/folder/"))
    hub.register(tmp_path / "work" / "gone")
    hub.projects[0].changes = 28
    hub.selected = 1
    rows = [visible(row) for row in hub.table_rows(width)]
    assert len(rows) == 3 and all(len(row) == width for row in rows)
    if width >= 120:
        assert rows[1].startswith(" › a-rather-long-project-name") and "SSH" in rows[1]
        assert rows[0].startswith("   alpha") and "LOCAL" in rows[0] and "28 changes" in rows[0]
        assert "folder not found" in rows[2]


def test_overview_without_projects_says_what_to_do(tmp_path):
    app = xsync.App(tmp_path / "projects.toml", screen=FakeScreen())
    assert "Press a to add one" in app.table_rows(120)[0]
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
    assert hub.view is hub.projects[1] and hub.view.touched
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


def test_adding_a_project_by_folder(hub, tmp_path, monkeypatch):
    beta = make_project_folder(tmp_path / "work" / "beta", "host:projects/beta/")
    (beta / "deep").mkdir()
    answers = iter([str(beta / "deep").replace("/", "/", 1), str(tmp_path / "work"), None])
    monkeypatch.setattr(hub, "ask", lambda label: next(answers))

    hub.on_overview_key("a")                              # a folder inside the project finds its root
    assert [project.root for project in hub.projects][-1] == beta and hub.selected == 1
    hub.on_overview_key("a")                              # no config in or above this folder
    assert len(hub.projects) == 2 and "No .xsync.toml" in hub.notes[0]
    hub.on_overview_key("a")                              # cancelled
    assert len(hub.projects) == 2


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


# --- setup info ----------------------------------------------------------------

@needs_rsync
def test_status_rows(project):
    version = subprocess.run([RSYNC[0], "--version"], capture_output=True, text=True).stdout
    config = {"remote": project.remote, "exclude": EXCLUDES, "rsync_args": ["--inplace"], "interval": 5}
    rows, ok = xsync.status_rows(project.src, config, project.remote, RSYNC, version)
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

    rows, ok = xsync.status_rows(project.src, config, "/", RSYNC, version)
    assert not ok
    assert dict(rows)["full sync"].startswith("refused")


# --- command line ----------------------------------------------------------------

def test_help_and_version(tmp_path):
    result = run_xsync(tmp_path, "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert run_xsync(tmp_path, "-h").stdout == result.stdout
    assert run_xsync(tmp_path, "--version").stdout.strip() == f"xsync {xsync.__version__}"


def test_init_creates_a_valid_starter_config(tmp_path):
    result = run_xsync(tmp_path, "--init")
    assert result.returncode == 0, result.stdout + result.stderr
    config = tomllib.loads((tmp_path / ".xsync.toml").read_text())
    assert set(config) == {"remote", "exclude"}
    assert config["remote"] == "myhost:/path/to/project/"
    assert ".xsync.files" in config["exclude"]
    assert run_xsync(tmp_path, "--init").returncode == 1  # never overwrites


def test_starter_config_optional_lines_are_valid_when_uncommented():
    lines = []
    for line in xsync.STARTER_CONFIG.splitlines():
        if line.startswith(("# interval =", "# rsync_args =")):
            line = line[2:]
        lines.append(line)
    config = tomllib.loads("\n".join(lines))
    assert config["interval"] == 2
    assert "--checksum" in config["rsync_args"]


def write_config(folder: Path, remote: str):
    (folder / ".xsync.toml").write_text(f'remote = "{remote}"\nexclude = []\n')


@pytest.mark.parametrize("args", [
    ["--sync"], ["--dry-run"], ["--watch"], ["--status"], ["--interval", "5"],
    ["a.txt"], ["--dryrun"], ["-n"], ["--verbose", "a.txt"],
])
def test_removed_and_unknown_arguments_are_errors_and_never_sync(project, args):
    write_config(project.src, project.remote)
    result = run_xsync(project.src, *args)
    assert result.returncode == 2
    assert "unknown argument" in result.stdout
    assert tree(project.dst) == set()


@pytest.mark.parametrize("args", [[], ["--verbose"]])
def test_without_a_terminal_nothing_is_synced(project, args):
    write_config(project.src, project.remote)
    result = run_xsync(project.src, *args)
    assert result.returncode == 1
    assert "needs a terminal" in result.stdout
    assert tree(project.dst) == set()


# --- optional: real SSH to localhost ---------------------------------------

@pytest.fixture
def ssh_localhost():
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip("ssh localhost is not available")


@needs_gnu_rsync
def test_ssh_sync_to_localhost(project, ssh_localhost):
    src, dst = project.src, project.dst
    remote = f"localhost:{dst}/"
    rsync = [*RSYNC, "-e", xsync.SSH_COMMAND]

    assert xsync.ssh_reachable(remote, rsync)
    assert xsync.sync_full(src, remote, project.exclude_file, False, rsync) == 0
    assert tree(dst) == {"a.txt", "sub/b.txt"}

    nested = src / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "it's a file.py").write_text("x")
    assert xsync.sync_path("deep/nested/it's a file.py", src, remote, project.exclude_file, False, rsync) == 0
    assert "deep/nested/it's a file.py" in tree(dst)

    shutil.rmtree(src / "deep")
    (src / "a.txt").unlink()
    rels = ["deep/nested/it's a file.py", "a.txt"]
    assert xsync.delete_from_remote(rels, src, remote, rsync) == 0
    assert tree(dst) == {"sub/b.txt"}
    assert not (dst / "deep").exists()  # emptied folders are removed too
