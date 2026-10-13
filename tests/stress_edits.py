#!/usr/bin/env python3
"""Make a burst of file changes in the current project, to watch xsync keep up.

Run it from inside a project that xsync is watching:

    python3 /path/to/xsync/tests/stress_edits.py            # 200 files, 3 s between phases
    python3 /path/to/xsync/tests/stress_edits.py --files 1000 --pause 0

Everything happens inside one new folder, xsync_stress_test/, in the current
directory. The script creates it, fills it, edits, renames and deletes files in
bursts, and removes the folder again at the end, so the project (and, with watch
mode on, the remote) ends up as it started. It refuses to run if the folder
already exists.

Each phase prints what it did and what xsync should show for it. With the
default pause every phase lands in its own watch batch; --pause 0 does it all
at once, the way a coding agent or a branch switch would.
"""

import argparse
import random
import shutil
import sys
import time
from pathlib import Path

FOLDER = "xsync_stress_test"


def main():
    parser = argparse.ArgumentParser(description="Make a burst of file changes in the current project.")
    parser.add_argument("--files", type=int, default=200, help="number of files to create (default 200)")
    parser.add_argument("--pause", type=float, default=3.0,
                        help="seconds between phases (default 3; 0 does everything at once)")
    parser.add_argument("--keep", action="store_true", help="leave the folder in place at the end")
    parser.add_argument("--seed", type=int, default=1, help="makes the choice of files repeatable (default 1)")
    args = parser.parse_args()

    root = Path.cwd() / FOLDER
    if root.exists():
        sys.exit(f"{root} already exists. Remove it first; this script only works in a folder it created.")
    random.seed(args.seed)
    started = time.monotonic()

    def phase(title: str, expect: str):
        print(f"{time.strftime('%H:%M:%S')}  {title}\n          xsync: {expect}", flush=True)
        time.sleep(args.pause)

    # 1. Create: files spread over nested folders
    folders = [root / f"pkg_{a}" / f"sub_{b}" for a in range(4) for b in range(3)] + [root]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    files = []
    for number in range(args.files):
        path = folders[number % len(folders)] / f"file_{number:04d}.txt"
        path.write_text(f"file {number}\n")
        files.append(path)
    made_folders = 1 + 4 + 12
    phase(f"created {len(files)} files in {made_folders} folders",
          f"{len(files) + made_folders} green + lines")

    # 2. Edit half of them, twice in a row (two writes to one file are one change to send)
    edited = random.sample(files, len(files) // 2)
    for path in edited:
        path.write_text(path.read_text() + "first edit\n")
        path.write_text(path.read_text() + "second edit, right after the first\n")
    phase(f"edited {len(edited)} files, twice each", f"{len(edited)} blue ~ lines")

    # 3. Overwrite the way editors and agents save: write a temp file, then rename it over the original
    replaced = random.sample(files, len(files) // 4)
    for path in replaced:
        temp = path.with_suffix(".tmp")
        temp.write_text(path.read_text() + "replaced through a temp file\n")
        temp.replace(path)
    phase(f"replaced {len(replaced)} files through a temp file and rename",
          f"{len(replaced)} blue ~ lines, and no .tmp files")

    # 4. Rename a quarter of the files
    renamed = random.sample(files, len(files) // 4)
    for path in renamed:
        target = path.with_name("renamed_" + path.name)
        path.rename(target)
        files[files.index(path)] = target
    phase(f"renamed {len(renamed)} files", f"{len(renamed)} red - lines and {len(renamed)} green + lines")

    # 5. Create and delete within the same instant: nothing should reach the remote
    for number in range(50):
        ghost = root / f"ghost_{number}.txt"
        ghost.write_text("gone before xsync looks\n")
        ghost.unlink()
    phase("created and immediately deleted 50 files", "nothing, or a few - lines for files that never arrived")

    # 6. Delete a third of the files one by one
    doomed = random.sample(files, len(files) // 3)
    for path in doomed:
        path.unlink()
        files.remove(path)
    phase(f"deleted {len(doomed)} files", f"{len(doomed)} red - lines")

    # 7. Delete a whole folder tree at once
    tree = root / "pkg_0"
    in_tree = [path for path in files if tree in path.parents]
    shutil.rmtree(tree)
    files = [path for path in files if path not in in_tree]
    phase(f"removed the folder pkg_0 with {len(in_tree)} files in it",
          f"{len(in_tree)} red - lines for the files, then 4 for the emptied folders")

    # 8. Clean up
    if args.keep:
        print(f"{time.strftime('%H:%M:%S')}  kept {root} with {len(files)} files (--keep)")
    else:
        left_folders = sum(1 for path in root.rglob("*") if path.is_dir()) + 1
        shutil.rmtree(root)
        phase(f"removed {FOLDER}/ with the remaining {len(files)} files",
              f"{len(files)} red - lines, then {left_folders} for the folders; the remote has no {FOLDER}/ left")

    print(f"done in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
