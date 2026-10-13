"""One project: its config, its watcher and queue, and what it is doing."""

import contextlib
import os
import re
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

from . import __version__
from .config import DEFAULT_INTERVAL, PLACEHOLDER_REMOTE, resolve_in_project, write_temp_file
from .excludes import make_excluded_checker
from .output import clock, emit, state, warn
from .remote import SSH_COMMAND, is_ssh_remote, normalize_remote, unsafe_remote_reason
from .rsync import (
    CANCELLED, CONNECTION_EXIT_CODES, RSYNC, delete_from_remote, is_gnu_rsync3, resolve_rsync, rsync_version,
    ssh_reachable, sync_batch, sync_full, sync_path,
)
from .ui import KEY_HELP, PROJECT_KEYS, Panel


# Seconds between attempts while a project is offline, and between checks of a host that is up
RETRY_SECONDS = 10
HEARTBEAT_SECONDS = 60
# After this many paths were sent in watch mode, the touched folders are checked once things
# have been quiet for SETTLE_SECONDS; fewer than that are forgotten after FORGET_SECONDS
BURST_PATHS = 100
SETTLE_SECONDS = 3
FORGET_SECONDS = 30
# For this long after a burst, any further change is followed by another check: the tail of a
# burst arrives in small batches, and those are as likely to have been reported incompletely
BURST_WINDOW_SECONDS = 60
IDLE = "in sync, watching for changes"
# Lines of history kept per project, so its list can be shown again after looking elsewhere
LOG_LINES = 2000


class Project:
    """One project: its config, its watcher and queue, its state and its list of lines.

    While one of its operations runs it is the active output (see App.activate),
    so everything emit() and warn() produce lands in its own log.
    """

    def __init__(self, root: Path, app):
        self.root = root
        self.name = root.name
        self.app = app
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.problems: deque[tuple[str, str]] = deque(maxlen=50)
        self.pending: set[str] = set()
        self.deleted: set[str] = set()
        self.lock = threading.Lock()
        self.observer = None
        self.offline = False
        self.changes = 0
        self.last_change = ""
        self.resting = ("idle, nothing checked yet", "busy")  # what the bar says when idle and not watching
        self.touched = False  # the user ran an action on it; only then is its host checked periodically
        self.scroll = 0  # lines the list on screen is scrolled back from its end; 0 follows new lines
        # Folders the watcher saw changes in, and how many paths were sent, since the last check
        # (see check_after_burst); last_event is when the watcher last reported anything
        self.touched_dirs: set[str] = set()
        self.unchecked = 0
        self.burst_at = float("-inf")  # when the last burst was seen
        self.last_event = time.monotonic()
        self.last_flush = self.last_beat = time.monotonic()
        self.error = ""  # why this project cannot be synced, if it cannot
        self.config: dict = {}
        self.remote = ""
        self.exclude_patterns: list[str] = []
        self.rsync = RSYNC
        self.version = ""
        self.gnu = False
        self.interval = DEFAULT_INTERVAL
        self.is_excluded = lambda path: True
        self.panel = Panel(self.name, PROJECT_KEYS)
        self.load()

    def load(self):
        """Read .xsync.toml and work out how this project syncs; sets self.error if it cannot."""
        problem = self._load()
        self.error = problem or ""
        self.panel.remote = self.remote
        self.panel.transport = ("ssh" if is_ssh_remote(self.remote) else "local") if self.remote else ""
        self.panel.state, self.panel.level = (problem, "problem") if problem else self.resting
        self.panel.detail = self.detail()
        self.sync_switches()

    def reload(self):
        """Read the config again after it was edited. Watch mode is switched off first."""
        self.app.activate(self)
        was_watching = self.watching
        self.close()
        self.offline = False
        self.remote = ""
        self.load()
        emit("config reloaded" + ("; watch mode was switched off" if was_watching else ""), "dim")
        if self.error:
            warn(self.error, "magenta")
        self.rest()

    def _load(self) -> str | None:
        if not self.root.is_dir():
            return "folder not found"
        try:
            with open(self.root / ".xsync.toml", "rb") as f:
                config = tomllib.load(f)
        except FileNotFoundError:
            return "no .xsync.toml in this folder"
        except tomllib.TOMLDecodeError as error:
            return f"invalid .xsync.toml: {error}"
        self.config = config
        if not config.get("remote"):
            return "'remote' is not set in .xsync.toml"
        if config["remote"] == PLACEHOLDER_REMOTE:
            return "set 'remote' in .xsync.toml first (press e to edit)"
        self.remote = normalize_remote(config["remote"])
        # Full syncs run rsync --delete
        reason = unsafe_remote_reason(self.remote)
        if reason:
            return f"refusing to sync with --delete: {reason}"
        try:
            self.interval = float(config.get("interval", DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            return "'interval' in .xsync.toml is not a number"
        self.exclude_patterns = list(config.get("exclude", []))
        self.is_excluded = make_excluded_checker(self.root, self.exclude_patterns)
        binary = resolve_rsync(config)
        version = rsync_version(binary)
        if version is None:
            return f"rsync not found: {binary}"
        self.version = version
        self.gnu = is_gnu_rsync3(version)
        extra_args = list(config.get("rsync_args", []))
        if is_ssh_remote(self.remote) and "RSYNC_RSH" not in os.environ:
            # Before the user's flags, so an -e in rsync_args still wins
            extra_args = ["-e", SSH_COMMAND, *extra_args]
        self.rsync = [binary, *extra_args]
        return None

    # --- output: this project's list and problem line

    def line(self, text: str):
        self.log.append(text)
        self.app.show_line(self, text)

    def waiting(self, seconds: float) -> bool:
        return self.app.while_waiting(self, seconds)

    def problem(self, text: str):
        self.problems.append((clock(), text))
        self.panel.message = f"last problem {clock()}: {text}   [p] shows all"
        self.panel.message_color = "magenta"
        self.app.refresh(self)

    # --- state shown in the bar and in the overview

    @property
    def watching(self) -> bool:
        return self.observer is not None

    @property
    def verbose(self) -> bool:
        return not (self.app.pretty and self.gnu)

    def detail(self) -> str:
        return f"{self.changes} changes" + (f" · last {self.last_change}" if self.last_change else "")

    def sync_switches(self):
        self.panel.switches = {"w": self.watching, "v": self.verbose}

    def set_status(self, state: str, level: str = "ok", mode: str | None = None):
        """Say what this project is doing. level is ok, busy or problem."""
        self.panel.state, self.panel.level = state, level
        self.panel.detail = self.detail()
        if mode is not None:
            self.panel.mode = mode
        self.sync_switches()
        self.app.refresh(self)

    def rest(self):
        """Set the status to what this project is doing when nothing is running."""
        if self.error:
            self.set_status(self.error, "problem", "idle")
        elif self.offline:
            queued = len(self.pending) + len(self.deleted)
            self.set_status(f"offline, {queued} queued, retrying", "problem", "offline")
        elif self.watching:
            self.set_status(IDLE, "ok", "watch")
        else:
            self.set_status(*self.resting, "idle")

    def count_changes(self, before: int) -> int:
        """Add what rsync sent since `before` to this project's counter."""
        sent = state.changes - before
        self.changes += sent
        if sent:
            self.last_change = clock()
        return sent

    def reached(self, codes: list[int]) -> bool:
        """Report rsync exit codes. False when the connection failed."""
        if any(code in CONNECTION_EXIT_CODES for code in codes):
            if not self.offline:
                self.offline = True
                warn(f"connection lost; retrying every {RETRY_SECONDS}s", "magenta")
            return False
        if self.offline:
            self.offline = False
            emit("reconnected", "green")
        for code in codes:
            if code == CANCELLED:
                emit("cancelled", "yellow")
            elif code != 0:
                warn(f"rsync exited with code {code}", "magenta")
        return True

    @contextlib.contextmanager
    def excludes(self):
        """The exclude list as a file for rsync, removed again afterwards."""
        path = write_temp_file("exclude", self.exclude_patterns)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    # --- actions

    def full_sync(self):
        self.app.activate(self)
        self.set_status("syncing…", "busy")
        before = state.changes
        with self.excludes() as exclude_file:
            rc = sync_full(self.root, self.remote, exclude_file, False, self.rsync, quiet=self.offline)
        if self.reached([rc]):
            if not self.count_changes(before) and rc == 0 and state.pretty:
                emit("already up to date", "dim")
            when = clock()[:5]
            if rc == 0:
                self.resting = (f"in sync at {when}", "ok")
            elif rc == CANCELLED:
                self.resting = (f"sync cancelled at {when}", "busy")
            else:
                self.resting = (f"sync had errors at {when}", "problem")
        self.rest()

    def compare(self):
        self.app.activate(self)
        self.set_status("comparing…", "busy")
        with self.excludes() as exclude_file:
            rc = sync_full(self.root, self.remote, exclude_file, True, self.rsync, quiet=self.offline)
        if self.reached([rc]):
            differences = sum(state.last_counts.values())
            when = clock()[:5]
            if rc == CANCELLED:
                self.resting = (f"compare cancelled at {when}", "busy")
            elif not state.pretty:
                self.resting = (f"compared at {when}", "busy")  # raw output: the count is not known
            elif differences:
                emit(f"{differences} difference(s), nothing changed; press s to sync", "dim")
                self.resting = (f"{differences} differences at {when}", "busy")
            elif rc == 0:
                emit("no differences", "dim")
                self.resting = (f"in sync at {when}", "ok")
        self.rest()

    def sync_one(self, text: str):
        """Sync the file or folder the user typed, pasted or dragged in."""
        self.app.activate(self)
        rel = resolve_in_project(text, self.root)
        if rel is None:
            warn(f"not found inside the project: {text.strip()}", "magenta")
            return
        self.set_status(f"syncing {rel}…", "busy")
        before = state.changes
        with self.excludes() as exclude_file:
            rc = sync_path(rel, self.root, self.remote, exclude_file, False, self.rsync)
        if self.reached([rc]) and not self.count_changes(before) and rc == 0 and state.pretty:
            emit(f"{rel}: already up to date", "dim")
        self.rest()

    def start_watch(self):
        self.app.activate(self)
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            warn("watch mode needs watchdog, which is not installed (pip install watchdog)", "magenta")
            return
        project = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                # Remember the folder each change was in, for check_after_burst
                project.last_event = time.monotonic()
                for abs_path in (event.src_path, getattr(event, "dest_path", "")):
                    # A folder counts as excluded if a file directly inside it would be
                    probe = str(Path(abs_path) / "x") if event.is_directory else abs_path
                    if not abs_path or project.is_excluded(probe):
                        continue
                    try:
                        folder = str(Path(abs_path).parent.relative_to(project.root))
                    except ValueError:
                        continue  # the project folder itself
                    with project.lock:
                        project.touched_dirs.add(folder)

            def _handle(self, abs_path: str):
                if project.is_excluded(abs_path):
                    return
                try:
                    rel = str(Path(abs_path).relative_to(project.root))
                except ValueError:
                    return
                with project.lock:
                    project.pending.add(rel)

            def on_modified(self, event):
                if not event.is_directory:
                    self._handle(event.src_path)

            def on_created(self, event):
                if not event.is_directory:
                    self._handle(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    # src is gone on remote; dest needs to be pushed
                    self._handle(event.dest_path)
                    # Excluded paths are never ours to delete on the remote
                    if project.is_excluded(event.src_path):
                        return
                    try:
                        rel = str(Path(event.src_path).relative_to(project.root))
                        with project.lock:
                            project.deleted.add(rel)
                    except ValueError:
                        pass

            def on_deleted(self, event):
                if event.is_directory:
                    return
                if project.is_excluded(event.src_path):
                    return
                try:
                    rel = str(Path(event.src_path).relative_to(project.root))
                except ValueError:
                    return
                with project.lock:
                    project.pending.discard(rel)
                    project.deleted.add(rel)

        # Start listening before the full sync, so nothing changed meanwhile is missed
        self.observer = Observer()
        self.observer.schedule(Handler(), str(self.root), recursive=True)
        self.observer.start()
        self.last_flush = time.monotonic()
        self.full_sync()

    def stop_watch(self):
        self.app.activate(self)
        self.close()
        emit("watch off", "dim")
        self.rest()

    def close(self):
        """Stop watching and forget what was queued."""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        with self.lock:
            self.pending.clear()
            self.deleted.clear()
            self.touched_dirs.clear()
        self.unchecked = 0
        self.burst_at = float("-inf")

    def flush(self):
        """Send what the watcher collected since the last flush."""
        with self.lock:
            batch = list(self.pending)
            self.pending.clear()
            removals = list(self.deleted)
            self.deleted.clear()

        # Anything "deleted" that still exists was overwritten (atomic write) — sync it
        overwritten = [f for f in removals if (self.root / f).exists()]
        removals = [f for f in removals if not (self.root / f).exists()]
        batch = list(set(batch) | set(overwritten))

        # Drop batch entries that no longer exist (temp files from atomic writes)
        batch = [f for f in batch if (self.root / f).exists()]

        if not batch and not removals:
            return
        self.app.activate(self)
        if not self.offline:
            self.set_status("syncing…", "busy")
        before = state.changes
        codes = []
        if removals:
            codes.append(delete_from_remote(removals, self.root, self.remote, self.rsync, quiet=self.offline))
        if batch:
            codes.append(sync_batch(batch, self.root, self.remote, rsync=self.rsync, quiet=self.offline))
        if CANCELLED in codes:
            # Carrying on would send a partial picture; a full sync on the next "watch on" catches up
            self.reached(codes)
            self.close()
            emit("watch mode switched off because the sync was cancelled", "yellow")
        elif self.reached(codes):
            self.count_changes(before)
            self.unchecked += len(batch) + len(removals)
            if self.unchecked >= BURST_PATHS:
                self.burst_at = time.monotonic()
        else:
            # Keep the changes and try again until the connection is back
            with self.lock:
                self.pending.update(batch)
                self.deleted.update(removals)
        self.rest()

    def check_after_burst(self):
        """Mirror the folders that saw changes, to catch what the watcher missed.

        When very many files change at once, the system's file watcher can skip
        events, and a file can be deleted between being queued and being sent.
        Either leaves the remote slightly off. So after a burst has settled,
        every touched folder is compared with its remote copy: what is directly
        inside it is sent or deleted as needed, exactly as a full sync would,
        without descending into subfolders that were not touched.
        """
        with self.lock:
            touched = set(self.touched_dirs)
            self.touched_dirs.clear()
        self.unchecked = 0
        folders = set()
        for rel in touched:
            folder = self.root / rel
            while not folder.is_dir() and folder != self.root:
                folder = folder.parent  # gone: its parent no longer lists it, so mirror that
            folders.add("./" if folder == self.root else f"{folder.relative_to(self.root)}/")
        if not folders:
            return
        self.app.activate(self)
        self.set_status("checking after a burst…", "busy")
        before = state.changes
        with self.excludes() as exclude_file:
            rc = sync_batch(sorted(folders), self.root, self.remote, rsync=self.rsync, quiet=self.offline,
                            mirror_excludes=exclude_file, recursive=False)
        if self.reached([rc]):
            fixed = self.count_changes(before)
            if fixed:
                self.burst_at = time.monotonic()  # it was not settled yet; keep checking a while longer
            emit(f"checked {len(folders)} folder(s) after a burst: " + (f"{fixed} correction(s)" if fixed else "all in order"), "dim")
        else:
            with self.lock:
                self.touched_dirs.update(touched)  # offline: check again once the connection is back
            self.unchecked = BURST_PATHS
        self.rest()

    def check_connection(self, announce: bool):
        """Probe the host; go offline or come back online accordingly."""
        self.app.activate(self)
        if not is_ssh_remote(self.remote):
            if announce:
                emit("local remote, no connection to check", "dim")
            return
        if announce:
            self.set_status("connecting…", "busy")
        was_offline = self.offline
        if ssh_reachable(self.remote, self.rsync, quiet=True):
            self.reached([0])
            if announce and not was_offline:
                emit("connection ok", "dim")
            if was_offline and self.watching:
                self.flush()
        else:
            self.reached([255])
        self.rest()

    def show_problems(self):
        self.app.activate(self)
        if not self.problems:
            emit("no problems recorded", "dim")
        for when, text in self.problems:
            emit(f"! {when}  {text}", "yellow")
        self.problems.clear()
        self.panel.message = ""
        self.app.refresh(self)

    def show_status(self):
        """List the resolved setup."""
        self.app.activate(self)
        self.set_status("checking…", "busy")
        rows, _ = status_rows(self.root, self.config, self.remote, self.rsync, self.version)
        for label, value in rows:
            emit(f"{label.ljust(11)} {value}", "dim")
        self.rest()

    def show_help(self):
        self.app.activate(self)
        emit(f"xsync {__version__}", "bold")
        for keys in PROJECT_KEYS:
            for key, _ in keys:
                emit(f"{key}   {KEY_HELP[key]}", "dim")
        emit("↑ ↓ PgUp PgDn Home End, or the mouse wheel: scroll back through this list", "dim")

    def tick(self, now: float):
        """Called a few times a second: send queued changes and check the host when due."""
        if self.error:
            return
        if self.watching and now - self.last_flush >= (RETRY_SECONDS if self.offline else self.interval):
            self.last_flush = now
            self.flush()
        quiet_for = time.monotonic() - self.last_event
        if self.watching and not self.offline and not self.pending and not self.deleted:
            in_burst = time.monotonic() - self.burst_at < BURST_WINDOW_SECONDS
            if self.unchecked >= (1 if in_burst else BURST_PATHS) and quiet_for >= SETTLE_SECONDS:
                self.check_after_burst()
            elif self.unchecked and quiet_for >= FORGET_SECONDS:
                # A few changes, long settled: the watcher is reliable at that pace
                with self.lock:
                    self.touched_dirs.clear()
                self.unchecked = 0
        # Notice a dropped connection without waiting for the next change
        in_use = self.touched or self.watching
        if in_use and now - self.last_beat >= (RETRY_SECONDS if self.offline else HEARTBEAT_SECONDS):
            self.last_beat = now
            self.check_connection(announce=False)


def status_rows(repo_root: Path, config: dict, remote: str, rsync: list[str], version: str) -> tuple[list, bool]:
    """The resolved setup as (label, value) rows, and whether a sync could run."""
    ssh = is_ssh_remote(remote)
    found = re.search(r"version\s+(\S+)", version)
    flavor = f"GNU rsync {found.group(1)}" if is_gnu_rsync3(version) and found else "not GNU rsync 3.x"
    try:
        import watchdog  # noqa: F401
        watching = "available"
    except ImportError:
        watching = "watchdog is not installed (pip install watchdog)"
    reason = unsafe_remote_reason(remote)
    if not ssh:
        reachable = "folder exists" if Path(remote).is_dir() else "folder not found"
    else:
        reachable = "reachable" if ssh_reachable(remote, rsync, quiet=True) else "not reachable"
    rows = [
        ("version", f"xsync {__version__}"),
        ("project", str(repo_root)),
        ("config", str(repo_root / ".xsync.toml")),
        ("remote", f"{remote}  ({'ssh' if ssh else 'local path'})"),
        ("host" if ssh else "target", reachable),
        ("full sync", "allowed" if reason is None else f"refused: {reason}"),
        ("rsync", f"{rsync[0]}  ({flavor})"),
        ("rsync_args", " ".join(config.get("rsync_args", [])) or "none"),
        ("exclude", f"{len(config.get('exclude', []))} patterns"),
        ("interval", f"{float(config.get('interval', DEFAULT_INTERVAL)):g}s between watch batches"),
        ("watch mode", watching),
    ]
    return rows, reason is None and reachable in ("reachable", "folder exists")
