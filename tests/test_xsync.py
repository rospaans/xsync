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
