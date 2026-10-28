"""Quick syncs from the record of what was sent, and mirroring to check the server itself."""

import os
from pathlib import Path

from xsync import project as project_module, sent as sent_module
from helpers import needs_gnu_rsync, tree


def bump(path: Path, text: str):
    """Change a file so its stamp differs, even within the clock's resolution."""
    path.write_text(text)
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))


@needs_gnu_rsync
def test_sync_sends_only_what_changed_since_the_last_one(hub, monkeypatch):
    project = hub.projects[0]
    dst = Path(project.remote)
    project.sync()                                        # no record yet: a mirror
    assert tree(dst) == {"a.txt", "sub/b.txt"}
    assert any("no record of an earlier sync" in line for line in project.log)
    assert project.record_file().exists()

    sent = []
    real_batch = project_module.sync_batch
    monkeypatch.setattr(project_module, "sync_batch", lambda batch, *args, **kw: (sent.append(batch), real_batch(batch, *args, **kw))[1])
    project.sync()
    assert sent == [] and "nothing changed" in project.log[-1]

    bump(project.root / "a.txt", "changed")
    (project.root / "sub" / "b.txt").unlink()
    (project.root / "new.txt").write_text("n")
    project.sync()
    assert sorted(sent[0]) == ["a.txt", "new.txt"]        # b.txt was not sent again
    assert tree(dst) == {"a.txt", "new.txt"} and (dst / "a.txt").read_text() == "changed"
    assert project.panel.state.startswith("in sync at")


@needs_gnu_rsync
def test_only_mirror_notices_changes_made_on_the_server(hub):
    project = hub.projects[0]
    dst = Path(project.remote)
    project.sync()
    (dst / "a.txt").unlink()                              # someone deleted it on the server
    project.sync()
    assert not (dst / "a.txt").exists()                   # sync trusts the record
    project.full_sync()
    assert (dst / "a.txt").exists()


@needs_gnu_rsync
def test_a_new_remote_starts_without_a_record(hub, tmp_path):
    project = hub.projects[0]
    project.sync()
    other = tmp_path / "remote" / "other"
    other.mkdir()
    project.remote = str(other) + "/"
    assert project.load_sent() is None
    project.sync()
    assert tree(other) == {"a.txt", "sub/b.txt"}


def test_scanning_skips_excluded_folders_and_says_where_the_many_files_are(hub, monkeypatch):
    project = hub.projects[0]
    (project.root / "ignored").mkdir()
    (project.root / "ignored" / "big.bin").write_text("x")
    environment = project.root / ".venv-other"
    environment.mkdir()
    for number in range(6):
        (environment / f"{number}.py").write_text("")
    files = sent_module.scan(project.root, project.is_excluded)
    assert "ignored/big.bin" not in files and ".xsync.toml" not in files and "sub/b.txt" in files
    monkeypatch.setattr(sent_module, "MANY_FILES", 5)
    hub.activate(project)
    project.scan()
    assert "most are in .venv-other/ (6)" in project.log[-1]


def test_a_damaged_record_means_a_mirror(tmp_path):
    path = tmp_path / "record.json"
    path.write_text("{not json")
    assert sent_module.load(path) is None
    sent_module.save(path, {"a.txt": (1, 2)})
    assert sent_module.load(path) == {"a.txt": (1, 2)}


def test_watch_sends_as_soon_as_changes_stop(hub, monkeypatch):
    """A save goes out once things are quiet, without waiting for the interval; a stream waits for it."""
    project = hub.projects[0]
    project.interval = 5
    flushed = []
    monkeypatch.setattr(project, "flush", lambda: flushed.append(True))
    now = 1000.0
    monkeypatch.setattr(project_module.time, "monotonic", lambda: now)
    project.last_flush, project.last_beat = now - 1, now
    project.observer = object()                            # as if watching
    try:
        project.last_event = now - 0.1                     # still changing
        project.tick(now)
        assert flushed == []
        project.last_event = now - 0.4                     # quiet now
        project.tick(now)
        assert flushed == [True]
        project.tick(now)                                  # nothing new since: no more batches
        assert flushed == [True]
    finally:
        project.observer = None
