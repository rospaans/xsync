"""What was last sent to a project's remote, so a sync can find what changed without reading the server.

The record maps each file's path to its modification time and size as they
were when it was sent. It is kept outside the project, one file per project
and remote, and only ever written after rsync reported success.
"""

import hashlib
import json
import os
from collections import Counter
from pathlib import Path


# Above this many files, a sync says where most of them are, in case those should be excluded
MANY_FILES = 5000


Stamp = tuple[int, int]  # modification time in nanoseconds, size in bytes


def record_path(folder: Path, root: Path, remote: str) -> Path:
    """Where the record for this project and remote lives; another remote starts without one."""
    key = hashlib.sha1(f"{root}\n{remote}".encode()).hexdigest()[:16]
    return folder / f"{root.name}-{key}.json"


def load(path: Path) -> dict[str, Stamp] | None:
    """The record, or None if there is none (or it cannot be read): then only a full sync will do."""
    try:
        files = json.loads(path.read_text())["files"]
        return {rel: (stamp[0], stamp[1]) for rel, stamp in files.items()}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def save(path: Path, files: dict[str, Stamp]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"files": files}, separators=(",", ":")))
    os.replace(temporary, path)  # never a half-written record


def scan(root: Path, is_excluded) -> dict[str, Stamp]:
    """Every file rsync would send, with its stamp. Only reads folder listings, never file contents.

    Excluded folders are skipped whole, so a large one costs nothing.
    """
    files = {}
    folders = [root]
    while folders:
        folder = folders.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    # A folder counts as excluded if a file directly inside it would be
                    if not is_excluded(str(path / "x")):
                        folders.append(path)
                    continue
                if is_excluded(entry.path):
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # gone meanwhile
            files[str(path.relative_to(root))] = (info.st_mtime_ns, info.st_size)
    return files


def crowded_folders(files: dict[str, Stamp], shown: int = 3) -> list[tuple[str, int]]:
    """The top-level folders holding the most files, with their counts."""
    counts = Counter(rel.split(os.sep, 1)[0] + "/" for rel in files if os.sep in rel)
    return counts.most_common(shown)


def size_text(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:,} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return ""
