"""Everything that runs rsync: full syncs, single paths, batches from watch mode, and deletes."""

import os
import re
import select
import subprocess
import time
from pathlib import Path

from .config import write_temp_file
from .output import emit, state, warn
from .remote import is_ssh_remote


HOMEBREW_RSYNC = "/opt/homebrew/bin/rsync"


# Default rsync command prefix: binary plus any extra args from the config
RSYNC = ["rsync"]


def resolve_rsync(config: dict) -> str:
    """Pick the rsync binary: config key, then Homebrew's, then whatever is on PATH."""
    configured = config.get("rsync")
    if configured:
        return os.path.expanduser(configured)
    return HOMEBREW_RSYNC if os.path.exists(HOMEBREW_RSYNC) else "rsync"


def is_gnu_rsync3(version_output: str) -> bool:
    """True if `rsync --version` output looks like GNU rsync 3.x (not openrsync)."""
    if "openrsync" in version_output:
        return False
    return re.match(r"rsync\s+version\s+3\.", version_output) is not None


def rsync_flags(remote: str) -> str:
    """Base rsync flags. Compression only pays off over the network."""
    flags = "-a" if state.pretty else "-av"
    return flags + "z" if is_ssh_remote(remote) else flags


def parse_change(line: str) -> tuple[str, str]:
    """Classify one line of `rsync --out-format='%i %n%L'` output.

    Returns (kind, text) with kind one of new, updated, deleted, skip, message.
    """
    if not line.strip():
        return "skip", ""
    if line.startswith("*deleting"):
        return "deleted", line[len("*deleting"):].strip()
    if len(line) > 12 and line[0] in "<>ch." and line[1] in "fdLDS" and line[11] == " ":
        code, name = line[:11], line[12:]
        if code[0] == ".":
            return "skip", name  # attributes only, no content sent
        return ("new" if "+++++++++" in code else "updated"), name
    return "message", line


# rsync's complaints about source files that are gone. When deleting remote
# copies of locally deleted files, that is the expected state, not a problem.
VANISHED_MESSAGES = ("file has vanished:", "WARNING: parent dir is absent", "rsync warning: some files vanished")


VANISHED_EXIT_CODE = 24


# rsync exit codes that mean the connection failed, so a retry can help:
# socket I/O, protocol stream, timeouts, and ssh's own failure code
CONNECTION_EXIT_CODES = {10, 12, 30, 35, 255}


CHANGE_MARKS = {"new": ("+", "green"), "updated": ("~", "blue"), "deleted": ("-", "red")}


# What run_rsync returns when the user cancelled it
CANCELLED = -1000


# Seconds between calls to state.output.waiting() while rsync runs: how quickly keys are answered
WAIT_SECONDS = 0.1


def run_rsync(cmd: list[str], quiet: bool = False) -> int:
    """Run rsync and turn its output into lines for the active project.

    With state.pretty that is one short line per change; otherwise rsync's own
    output. quiet hides rsync's messages and errors (changes are still shown).
    While rsync runs, state.output.waiting() is called regularly so the screen stays
    alive; if it returns True rsync is stopped and CANCELLED is returned.
    """
    dry_run = "--dry-run" in cmd
    if state.pretty:
        cmd = [cmd[0], "--out-format=%i %n%L", "--outbuf=L", *cmd[1:]]
    counts = {"new": 0, "updated": 0, "deleted": 0}
    seen_messages = set()
    deleting_missing = "--delete-missing-args" in cmd

    def handle(line: str):
        if not state.pretty:
            if line.strip() and not quiet:
                emit(line, "dim")
            return
        kind, text = parse_change(line)
        if kind in counts:
            counts[kind] += 1
            mark, color = CHANGE_MARKS[kind]
            emit(f"{mark} {text}", color, note="not synced" if dry_run else "")
        elif kind == "message" and not quiet and text not in seen_messages:
            if deleting_missing and text.startswith(VANISHED_MESSAGES):
                return
            seen_messages.add(text)
            warn(text)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = last_wait = time.monotonic()
    cancelled = False
    unfinished = b""
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], WAIT_SECONDS)
        if ready:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            *lines, unfinished = (unfinished + data).split(b"\n")
            for line in lines:
                handle(line.decode("utf-8", "replace"))
        elif proc.poll() is not None:
            # rsync is done once it has exited and nothing is left to read. Waiting for the pipe
            # to close would hang when ssh leaves a background process holding it (ControlPersist).
            break
        # Also while rsync keeps printing (a first sync lists every file): otherwise keys and
        # the timer would only be seen once it pauses
        now = time.monotonic()
        if not cancelled and now - last_wait >= WAIT_SECONDS:
            last_wait = now
            if state.output.waiting(now - started):
                cancelled = True
                proc.terminate()
    if unfinished:
        handle(unfinished.decode("utf-8", "replace"))
    proc.stdout.close()
    rc = proc.wait()
    state.last_counts.update(counts)
    if not dry_run:
        state.changes += sum(counts.values())
    return CANCELLED if cancelled else rc


def sync_full(repo_root: Path, remote: str, exclude_file: Path, dry_run: bool,
              rsync: list[str] = RSYNC, quiet: bool = False) -> int:
    """Sync entire repo to remote."""
    cmd = [
        rsync[0],
        rsync_flags(remote),
        *rsync[1:],  # extra args go after -a so they can override it
        "--delete",
        f"--exclude-from={exclude_file}",
        str(repo_root) + "/",
        remote,
    ]
    if dry_run:
        cmd.insert(1, "--dry-run")
    return run_rsync(cmd, quiet)


def sync_path(rel: str, repo_root: Path, remote: str, exclude_file: Path, dry_run: bool,
              rsync: list[str] = RSYNC) -> int:
    """Sync one file, or mirror one folder, keeping its relative path on the remote."""
    if rel == ".":
        return sync_full(repo_root, remote, exclude_file, dry_run, rsync)
    mirror = exclude_file if (repo_root / rel).is_dir() else None
    return sync_batch([rel], repo_root, remote, dry_run, rsync, mirror_excludes=mirror)


def inside_remote(rels: list[str]) -> list[str]:
    """Drop relative paths that could resolve outside the remote project directory."""
    safe = []
    for rel in rels:
        if not rel or rel.startswith("/") or ".." in Path(rel).parts or "\n" in rel or "\r" in rel:
            print(f"warning: skipping path outside the project: {rel!r}")
        else:
            safe.append(rel)
    return safe


def sync_batch(batch: list[str], repo_root: Path, remote: str, dry_run: bool = False,
               rsync: list[str] = RSYNC, delete_missing: bool = False, quiet: bool = False,
               mirror_excludes: Path | None = None, recursive: bool = True) -> int:
    """Sync a batch of relative paths using rsync --files-from.

    --files-from implies --relative, so missing parent directories are created.
    With delete_missing, listed files that no longer exist locally are deleted
    on the remote. With mirror_excludes (an exclude file), listed folders are
    mirrored like a full sync, with remote extras deleted: whole trees, or with
    recursive=False only what is directly inside each folder (list them with a
    trailing slash; subfolders that exist on both sides are left alone).
    """
    batch = inside_remote(batch)
    if not batch:
        return 0
    files_from = write_temp_file("files", batch)
    cmd = [
        rsync[0],
        rsync_flags(remote),
        *rsync[1:],  # extra args go after -a so they can override it
        *((["-r"] if recursive else []) + ["--delete", f"--exclude-from={mirror_excludes}"] if mirror_excludes else []),
        "--files-from", str(files_from),
        str(repo_root) + "/",
        remote,
    ]
    if dry_run:
        cmd.insert(1, "--dry-run")
    if delete_missing:
        cmd.insert(1, "--delete-missing-args")
    try:
        rc = run_rsync(cmd, quiet)
    finally:
        files_from.unlink(missing_ok=True)
    return 0 if delete_missing and rc == VANISHED_EXIT_CODE else rc


def ssh_reachable(remote: str, rsync: list[str] = RSYNC, quiet: bool = False) -> bool:
    """Check that the SSH host answers without prompting for a password.

    Probes through rsync instead of running a remote command, so it also
    works with keys that are restricted to rsync (rrsync).
    """
    host = remote.split(":", 1)[0]
    # -T: no terminal, even if ~/.ssh/config sets RequestTTY for this host
    ssh = "ssh -T -o BatchMode=yes -o ConnectTimeout=5"
    cmd = [rsync[0], "-e", ssh, "--list-only", f"{host}:."]
    errors = subprocess.DEVNULL if quiet else None
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=errors).returncode == 0


def missing_parent_dirs(rels: list[str], repo_root: Path) -> list[list[str]]:
    """Folders above the given paths that no longer exist locally, grouped deepest first."""
    by_depth: dict[int, set[str]] = {}
    for rel in rels:
        for parent in Path(rel).parents:
            if parent.parts and not (repo_root / parent).exists():
                by_depth.setdefault(len(parent.parts), set()).add(str(parent))
    return [sorted(by_depth[depth]) for depth in sorted(by_depth, reverse=True)]


def delete_from_remote(deleted_rels: list[str], repo_root: Path, remote: str,
                       rsync: list[str] = RSYNC, quiet: bool = False) -> int:
    """Delete specific files from remote without a full rsync scan.

    Folders that disappeared locally are removed too, but only once they are
    empty on the remote. Over SSH this goes through rsync (--delete-missing-args,
    GNU rsync 3.1+), so no shell command ever runs on the remote host.
    """
    rels = inside_remote(deleted_rels)
    folders = missing_parent_dirs(rels, repo_root)
    if is_ssh_remote(remote):
        rc = sync_batch(rels, repo_root, remote, rsync=rsync, delete_missing=True, quiet=quiet)
        if rc == 0:
            # One pass per depth: rsync only removes a folder that is already empty,
            # and stays silent here about folders that still hold remote-only files
            for level in folders:
                sync_batch(level, repo_root, remote, rsync=rsync, delete_missing=True, quiet=True)
        return rc
    # A local remote: delete directly, reporting each removal like rsync would
    base = Path(remote.rstrip("/"))
    removed = []
    for rel in rels:
        if (base / rel).is_file() or (base / rel).is_symlink():
            (base / rel).unlink()
            removed.append(rel)
    for level in folders:
        for folder in level:
            try:
                (base / folder).rmdir()
                removed.append(folder + "/")
            except OSError:
                pass  # not empty, or already gone
    for rel in removed:
        emit(f"- {rel}", "red")
    state.changes += len(removed)
    return 0


_RSYNC_VERSIONS: dict[str, str | None] = {}


def rsync_version(binary: str) -> str | None:
    """The output of `<binary> --version`, or None if it cannot be run. Asked once per binary."""
    if binary not in _RSYNC_VERSIONS:
        try:
            _RSYNC_VERSIONS[binary] = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout
        except OSError:
            _RSYNC_VERSIONS[binary] = None
    return _RSYNC_VERSIONS[binary]
