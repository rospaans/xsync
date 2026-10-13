"""Files xsync reads and writes: a project's .xsync.toml, and the list of known projects."""

import fcntl
import json
import os
import shlex
import tempfile
import tomllib
from pathlib import Path


# The remote in the starter config; a project still using it has not been set up
PLACEHOLDER_REMOTE = "myhost:/path/to/project/"


STARTER_CONFIG = """\
# xsync configuration. The folder containing this file is what gets synced.

# Where to sync to: "<ssh alias>:<folder on the server>/"
remote = "myhost:/path/to/project/"

# Files and folders to skip (rsync exclude syntax)
exclude = [
    ".git/",
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    ".ipynb_checkpoints/",
    "*.egg-info/",
    ".pytest_cache/",
    ".ruff_cache/",
    "node_modules/",
    "data/",
    "outputs/",
    "logs/",
    ".xsync.toml",
]

# --- Optional settings: remove the leading "# " from a line to use it ---
# A local path instead of SSH (replaces the remote line above)
# remote = "/Volumes/your-volume/your-project/"

# Seconds watch mode collects changes before sending them (default 2)
# interval = 2

# Extra rsync flags. This set is for a server folder on a network share (CIFS/SMB)
# that rejects permission, ownership and timestamp changes; see the README.
# rsync_args = ["--no-perms", "--no-owner", "--no-group", "--no-times", "--omit-dir-times", "--inplace", "--checksum"]
"""


def find_root(start: Path) -> Path | None:
    """Walk up from start, looking for the folder that holds .xsync.toml."""
    for directory in [start, *start.parents]:
        if (directory / ".xsync.toml").exists():
            return directory
    return None


def write_temp_file(kind: str, entries: list[str]) -> Path:
    """Write a list for rsync (exclude patterns, or paths to send) to a temporary file.

    The file goes in the system's temp folder, never in the project, so it is
    not picked up by the file watcher or by a sync. The caller removes it.
    """
    handle, name = tempfile.mkstemp(prefix="xsync-", suffix=f".{kind}", text=True)
    with os.fdopen(handle, "w") as f:
        f.write("\n".join(entries) + "\n")
    return Path(name)


def acquire_lock(path: Path):
    """Make sure only one xsync runs at a time; two would sync the same projects over each other.

    Returns the open lock file (keep it for as long as xsync runs), or None
    if another xsync already holds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def resolve_in_project(text: str, repo_root: Path) -> str | None:
    """Turn a typed, pasted or dragged path into a path relative to the project root.

    Accepts absolute paths and paths relative to the current directory or the
    project root. Returns None if it does not exist or lies outside the project.
    """
    text = text.strip()
    try:
        parts = shlex.split(text)  # undo quotes and backslash-escaped spaces from drag and drop
    except ValueError:
        parts = []
    if len(parts) == 1:
        text = parts[0]
    if not text:
        return None
    path = Path(os.path.expanduser(text))
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, repo_root / path]
    for candidate in candidates:
        if candidate.exists():
            try:
                return str(candidate.resolve().relative_to(repo_root.resolve()))
            except ValueError:
                continue
    return None


# Temp files that older versions of xsync wrote into the project folder; never synced or watched
INTERNAL_FILES = {".xsync.files", ".xsync.exclude"}


# Seconds watch mode collects changes before sending them; `interval` in .xsync.toml overrides it
DEFAULT_INTERVAL = 2.0


def registry_path() -> Path:
    """The file that remembers which projects exist, in the user's config folder."""
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "xsync" / "projects.toml"


def load_registry(path: Path) -> list[Path]:
    try:
        with open(path, "rb") as f:
            entries = tomllib.load(f).get("projects", [])
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return []
    return [Path(entry) for entry in entries if isinstance(entry, str)]


def save_registry(path: Path, roots: list[Path]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Projects xsync knows about. Managed by xsync: a project is added when you run", 
             "# xsync inside it, or with the a key in the overview; the x key removes one.",
             "projects = ["]
    lines += [f"    {json.dumps(str(root))}," for root in roots]
    path.write_text("\n".join(lines + ["]"]) + "\n")


def clean_path(text: str) -> str:
    """Undo the quotes and backslash-escaped spaces of a pasted or dragged path."""
    text = text.strip()
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = []
    return os.path.expanduser(parts[0] if len(parts) == 1 else text)
