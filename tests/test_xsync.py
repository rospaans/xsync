"""Tests for xsync. Run with: python -m pytest tests"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
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

needs_rsync = pytest.mark.skipif(
    shutil.which(xsync.resolve_rsync({})) is None, reason="rsync not available"
)


def _default_rsync_is_gnu() -> bool:
    try:
        out = subprocess.run([xsync.resolve_rsync({}), "--version"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return xsync.is_gnu_rsync3(out)


needs_gnu_rsync = pytest.mark.skipif(not _default_rsync_is_gnu(), reason="needs GNU rsync 3.x")


def run_xsync(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, str(XSYNC_PATH), *args],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def write_config(src: Path, remote: str, extra: str = ""):
    (src / ".xsync.toml").write_text(
        f"remote = {json.dumps(remote)}\nexclude = {json.dumps(EXCLUDES)}\n{extra}"
    )


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


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


# --- awkward filenames -----------------------------------------------------

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
    rsync = [xsync.resolve_rsync({})]
    remote = str(dst) + "/"

    assert xsync.sync_batch(NASTY_NAMES + ["keep.txt"], src, remote, rsync=rsync) == 0
    assert tree(dst) == set(NASTY_NAMES) | {"keep.txt"}

    for name in NASTY_NAMES:
        (src / name).unlink()
    assert xsync.sync_batch(NASTY_NAMES, src, remote, rsync=rsync, delete_missing=True) == 0
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
    rsync = [xsync.resolve_rsync({})]

    rc = xsync.sync_batch(["gone/a.txt"], src, str(dst) + "/", rsync=rsync, delete_missing=True)
    assert rc == 0
    assert tree(dst) == {"keep.txt"}


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
    rc = xsync.sync_batch(["../up.txt", "a.txt"], src, str(remote) + "/", rsync=[xsync.resolve_rsync({})])
    assert rc == 0
    assert tree(tmp_path / "parent") == {"remote/a.txt"}


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


# --- output formatting ------------------------------------------------------

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
def test_summary_output(project):
    src, dst = project
    first = run_xsync(src, "--dry-run")
    assert "  + a.txt" in first.stdout and "would be new" in first.stdout
    assert tree(dst) == set()

    assert "  + sub/b.txt" in run_xsync(src).stdout
    assert "Already up to date" in run_xsync(src).stdout

    (src / "a.txt").write_text("changed content")
    (src / "sub" / "b.txt").unlink()
    result = run_xsync(src)
    assert "  ~ a.txt" in result.stdout
    assert "  - sub/b.txt" in result.stdout
    assert "1 updated, 1 deleted" in result.stdout
    assert "sending incremental file list" not in result.stdout


@needs_gnu_rsync
def test_verbose_shows_raw_rsync_output(project):
    src, dst = project
    result = run_xsync(src, "--verbose")
    assert result.returncode == 0
    assert "sending incremental file list" in result.stdout
    assert tree(dst) == {"a.txt", "sub/b.txt"}


def test_format_header_plain():
    assert xsync.format_header("sync", "proj", "host:/srv/proj/", "ssh") == "xsync sync  proj → host:/srv/proj/  ssh"
    assert xsync.format_header("watch", "proj", "host:/srv/proj/") == "xsync watch  proj → host:/srv/proj/"


@pytest.mark.parametrize("width", [20, 40, 60, 80, 200])
def test_format_header_fits_width(width):
    line = xsync.format_header("watch", "a_long_project_name", "host:some/quite/long/remote/folder/", "ssh · 12 changes", width)
    assert len(line) <= max(width, len("xsync watch") + 3)
    assert line.startswith("xsync watch")


@pytest.mark.parametrize("width", [25, 40, 80, 120, 200])
def test_session_rows_never_exceed_terminal_width(width):
    import re
    screen = xsync.Screen("a_project", "host:some/long/remote/folder/", "ssh", interactive=False)
    states = [("watch", xsync.IDLE, "ok", "WATCH"), ("idle", "3 differences at 15:44", "busy", "XSYNC"),
              ("offline", "offline, 3 queued, retrying", "problem", "OFFLINE")]
    for mode, state, level, label in states:
        screen.mode, screen.state, screen.level, screen.detail = mode, state, level, "28 changes · last 15:44:16"
        bar = re.sub(r"\033\[[0-9;]*m", "", screen.bar(width))
        assert len(bar) <= width
        assert bar.startswith(f" {label} ")
    screen.message = "last problem 15:44:16: " + "x" * 300
    for row in (screen.key_row(width), screen.message_row(width)):
        assert len(re.sub(r"\033\[[0-9;]*m", "", row)) <= width


def test_key_row_lists_every_key_and_the_watch_state():
    screen = xsync.Screen("p", "host:x/", "ssh", interactive=False)
    row = screen.key_row(200)
    for key, _ in xsync.SESSION_KEYS:
        assert f"[{key}]" in row
    assert "Watch off" in row
    screen.watching = True
    assert "Watch on" in screen.key_row(200)


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
    rsync = [xsync.resolve_rsync({})]
    remote = str(dst) + "/"
    rels = ["gone/deep/a.txt", "shared/b.txt"]

    assert xsync.sync_batch(rels, src, remote, rsync=rsync, delete_missing=True) == 0
    for level in xsync.missing_parent_dirs(rels, src):
        xsync.sync_batch(level, src, remote, rsync=rsync, delete_missing=True, quiet=True)

    assert not (dst / "gone").exists()
    assert tree(dst) == {"shared/remote_only.txt"}


@needs_rsync
def test_status_reports_without_syncing(project):
    src, dst = project
    result = run_xsync(src, "--status")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "xsync status" in result.stdout
    assert str(dst) in result.stdout
    assert "folder exists" in result.stdout
    assert tree(dst) == set()


def test_version(project):
    src, dst = project
    result = run_xsync(src, "--version")
    assert result.stdout.strip() == f"xsync {xsync.__version__}"
    assert tree(dst) == set()


# --- end to end ------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    """A source project with a few files, and an empty local destination."""
    tmp_path = tmp_path.resolve()
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "ignored").mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")
    (src / "ignored" / "c.txt").write_text("c")
    write_config(src, str(dst) + "/")
    return src, dst


@needs_rsync
def test_full_sync(project):
    src, dst = project
    result = run_xsync(src)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"a.txt", "sub/b.txt"}


@needs_rsync
def test_rsync_args_are_passed_through(project):
    src, dst = project
    write_config(src, str(dst) + "/", 'rsync_args = ["--exclude=a.txt"]\n')
    result = run_xsync(src)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"sub/b.txt"}


@needs_rsync
def test_rsync_args_can_override_archive_flags(project):
    src, dst = project
    os.utime(src / "a.txt", (1_000_000_000, 1_000_000_000))  # 2001
    write_config(src, str(dst) + "/", 'rsync_args = ["--no-times"]\n')
    result = run_xsync(src)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (dst / "a.txt").stat().st_mtime > 1_500_000_000  # not copied from source


@pytest.mark.parametrize("args", [["--help"], ["-h"], ["--dryrun"], ["-n"], ["a.txt", "--bogus"], ["a.txt", "sub/b.txt"]])
def test_help_and_bad_arguments_never_sync(project, args):
    src, dst = project
    result = run_xsync(src, *args)
    assert tree(dst) == set()
    if args[0] in ("--help", "-h"):
        assert result.returncode == 0
        assert "Usage:" in result.stdout
    else:
        assert result.returncode == 2


def test_starter_config_is_valid_toml():
    import tomllib
    config = tomllib.loads(xsync.STARTER_CONFIG)
    assert config["remote"] == "myhost:/path/to/project/"
    assert set(config) == {"remote", "exclude"}
    assert ".xsync.files" in config["exclude"]


@needs_rsync
def test_sync_flag_syncs_once(project):
    src, dst = project
    result = run_xsync(src, "--sync")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"a.txt", "sub/b.txt"}


@needs_rsync
def test_folder_sync_mirrors_only_that_folder(project):
    src, dst = project
    (src / "sub" / "deeper").mkdir()
    (src / "sub" / "deeper" / "c.txt").write_text("c")
    (src / "sub" / "ignored").mkdir()
    (src / "sub" / "ignored" / "skip.txt").write_text("s")
    (dst / "sub").mkdir()
    (dst / "sub" / "stale.txt").write_text("old")
    (dst / "elsewhere.txt").write_text("keep")

    preview = run_xsync(src, "sub", "--dry-run")
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert tree(dst) == {"sub/stale.txt", "elsewhere.txt"}

    result = run_xsync(src, "sub")
    assert result.returncode == 0, result.stdout + result.stderr
    # sub/ is mirrored (stale file gone, excludes respected); nothing outside it is touched
    assert tree(dst) == {"sub/b.txt", "sub/deeper/c.txt", "elsewhere.txt"}


@needs_rsync
def test_path_outside_project_is_rejected(project, tmp_path):
    src, dst = project
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    result = run_xsync(src, str(outside))
    assert result.returncode == 1
    assert "not found inside the project" in result.stdout
    assert tree(dst) == set()


@needs_rsync
def test_full_sync_from_subdirectory(project):
    src, dst = project
    result = run_xsync(src / "sub")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"a.txt", "sub/b.txt"}


@needs_rsync
def test_single_file_sync_into_missing_nested_folder(project):
    src, dst = project
    nested = src / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "f.py").write_text("print('hi')\n")

    result = run_xsync(src, "deep/nested/f.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"deep/nested/f.py"}


@needs_rsync
def test_dry_run_changes_nothing(project):
    src, dst = project
    before = tree(src)

    full = run_xsync(src, "--dry-run")
    single = run_xsync(src, "sub/b.txt", "--dry-run")

    assert full.returncode == 0, full.stdout + full.stderr
    assert single.returncode == 0, single.stdout + single.stderr
    assert tree(dst) == set()
    assert tree(src) == before  # temp files cleaned up


@needs_rsync
def test_deletion_propagates(project):
    src, dst = project
    assert run_xsync(src).returncode == 0
    (src / "a.txt").unlink()
    assert run_xsync(src).returncode == 0
    assert tree(dst) == {"sub/b.txt"}


def test_delete_from_local_remote(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / "keep.txt").write_text("k")
    rc = xsync.delete_from_remote(["sub/b.txt", "missing.txt"], tmp_path, str(tmp_path) + "/")
    assert rc == 0
    assert tree(tmp_path) == {"keep.txt"}


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
    src, dst = project
    write_config(src, f"localhost:{dst}/")
    remote = f"localhost:{dst}/"

    result = run_xsync(src)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(dst) == {"a.txt", "sub/b.txt"}

    nested = src / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "it's a file.py").write_text("x")
    result = run_xsync(src, "deep/nested/it's a file.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deep/nested/it's a file.py" in tree(dst)

    (nested / "it's a file.py").unlink()
    (src / "a.txt").unlink()
    rels = ["deep/nested/it's a file.py", "a.txt"]
    assert xsync.delete_from_remote(rels, src, remote, [xsync.resolve_rsync({})]) == 0
    assert tree(dst) == {"sub/b.txt"}
