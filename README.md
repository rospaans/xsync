# xsync

[![CI](https://github.com/rospaans/xsync/actions/workflows/ci.yml/badge.svg)](https://github.com/rospaans/xsync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A small `rsync` wrapper for syncing a local project directory to a remote destination. One command, one config file per project.

## What it does

`xsync` reads a `.xsync.toml` file from the project root and runs `rsync` against the `remote` target declared there. The logic lives in a single script on your `$PATH`; each project only needs a declarative config. It works from any terminal and any editor.

- Full sync: mirrors the project to the remote, respecting an exclude list.
- Single-file sync: pushes one file while preserving its relative path.
- Dry-run mode: preview what would change without transferring.

## Requirements

- Python 3.11+ (for `tomllib` in the standard library)
- `rsync` (preinstalled on macOS and most Linux distributions)

## Installation

```bash
git clone https://github.com/rospaans/xsync.git
cd xsync
bash install.sh
```

The installer copies `xsync` to `~/.local/bin` and, if needed, adds it to your `PATH` in `~/.zshrc` or `~/.bashrc`. Restart your shell or `source` the rc file afterwards.

## Usage

```bash
xsync                      # sync entire project to remote
xsync path/to/file.py      # sync a single file
xsync --init               # create a starter .xsync.toml in current dir
xsync --dry-run            # show what would sync, no transfer
xsync file.py --dry-run    # dry-run single-file mode
```

`xsync` walks up from the current directory to find `.xsync.toml`, so you can run it from any subdirectory of the project.

## Configuration

`.xsync.toml` lives at the project root. Copy from `.xsync.toml.example` or generate one with `xsync --init`.

```toml
# Remote destination: local path (e.g. mounted volume) or SSH target
remote = "/Volumes/your-volume/your-project/"
# remote = "user@host:/path/to/project/"

# Patterns to exclude (rsync exclude-from syntax)
exclude = [
    ".git/",
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    "node_modules/",
    "data/",
    "outputs/",
]
```

The `remote` field accepts anything `rsync` accepts: a local path (mounted volume, external drive) or an SSH target in the form `user@host:/path/`.

## How it works

- **Full sync** (`xsync`): invokes `rsync -avz --delete` with the exclude list. The remote becomes an exact mirror of the local project, minus excluded paths.
- **Single-file sync** (`xsync path/to/file`): transfers just that file, preserving its relative path under the remote root.

## Setting up a new project

```bash
cd /path/to/my_project
xsync --init           # creates .xsync.toml
# edit .xsync.toml: set `remote`, adjust excludes
xsync --dry-run        # verify what would sync
xsync                  # first real sync
```

Add `.xsync.toml` to the project's `.gitignore` if the remote path is personal, or commit it if it is generic enough to share.

## Troubleshooting

**No `.xsync.toml` found**
Either `cd` into a project that has one, or run `xsync --init` to create one here.

**Remote not found**
The mounted volume or SSH host is not reachable. Mount the volume or verify the host, then retry.

**File is outside repo**
The file path passed to `xsync` is not inside the project root. `xsync` only syncs files within the project.

**Exit code 23 from rsync**
Some files could not be transferred (permissions, vanished files). The rest of the sync completed. Usually safe to ignore.

## License

[MIT](LICENSE) © Robert Spaans
