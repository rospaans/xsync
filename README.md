# xsync

[![CI](https://github.com/rospaans/xsync/actions/workflows/ci.yml/badge.svg)](https://github.com/rospaans/xsync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A small `rsync` wrapper for syncing a local project directory to a remote destination. One command, one config file per project.

## Recent changes: hardened for SSH remotes

SSH remotes (`host:/path/`, with `host` an alias from `~/.ssh/config`) are now the primary setup. Local paths keep working.

- **Safe remote deletes**: `--watch` quotes deleted paths with `shlex.quote` and passes them after `rm -f --`, so odd filenames can no longer break or inject commands. A failed delete prints a warning.
- **Single-file sync creates missing folders**: `xsync path/to/new/file.py` works even if `path/to/new/` does not exist on the remote yet.
- **`--delete` guard**: full syncs refuse to run when the remote path is empty, `/`, `~`, `.` or a home directory. A missing trailing `/` on `remote` is added automatically.
- **Compression only over SSH**: `-z` is dropped for local and mounted paths.
- **Fail fast**: an unreachable SSH host (down, key not loaded, VPN off) is reported within 5 seconds, before any sync and before `--watch` starts.
- **rsync flavor**: new optional `rsync = "/path/to/rsync"` key. Defaults to `/opt/homebrew/bin/rsync` if present, else `rsync` from `PATH`. A one-line warning is printed if it is not GNU rsync 3.x (recent macOS ships openrsync).
- **Extra rsync flags**: new optional `rsync_args = [...]` key, for remotes on storage that rejects permission, ownership or timestamp changes.
- **Tests**: `python -m pytest tests`.

## What it does

`xsync` reads a `.xsync.toml` file from the project root and runs `rsync` against the `remote` target declared there. The logic lives in a single script on your `$PATH`; each project only needs a declarative config. It works from any terminal and any editor.

- Full sync: mirrors the project to the remote, respecting an exclude list.
- Single-file sync: pushes one file while preserving its relative path.
- Dry-run mode: preview what would change without transferring.

## Requirements

- Python 3.11+ (for `tomllib` in the standard library)
- `rsync`, preferably GNU rsync 3.x (`brew install rsync` on macOS, which ships openrsync)
- For SSH remotes: key-based login to a host alias defined in `~/.ssh/config`
- Optional: `watchdog` for `--watch` (installed by `install.sh`)

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
xsync --watch              # watch for changes and sync automatically
xsync --watch --interval 5 # watch with a 5-second debounce window
```

`xsync` walks up from the current directory to find `.xsync.toml`, so you can run it from any subdirectory of the project.

## Configuration

`.xsync.toml` lives at the project root. Copy from `.xsync.toml.example` or generate one with `xsync --init`.

```toml
# Remote destination: SSH target, where the host is an alias from ~/.ssh/config
remote = "myhost:/path/to/project/"
# Alternative: a local path (e.g. mounted volume)
# remote = "/Volumes/your-volume/your-project/"

# rsync binary to use (default: /opt/homebrew/bin/rsync if present, else rsync from PATH)
# rsync = "/opt/homebrew/bin/rsync"

# Extra rsync flags, e.g. when the remote directory is a network share that
# rejects permission, ownership or timestamp changes
# rsync_args = ["--no-perms", "--no-owner", "--no-group", "--omit-dir-times", "--inplace"]

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

The `remote` field is an SSH target in the form `host:/path/` (or `user@host:/path/`), or a local path (mounted volume, external drive). For SSH, define the host in `~/.ssh/config` with key authentication; `ControlMaster` multiplexing makes repeated syncs and `--watch` much faster.

## How it works

- **Full sync** (`xsync`): invokes `rsync -av --delete` with the exclude list (plus `-z` over SSH). The remote becomes an exact mirror of the local project, minus excluded paths.
- **Single-file sync** (`xsync path/to/file`): transfers just that file, preserving its relative path under the remote root and creating missing folders.
- **Watch** (`xsync --watch`): one full sync, then pushes changed files in batches and removes deleted files from the remote.

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

**Cannot reach host over SSH**
The host is down, your key is not loaded (`ssh-add`), or the VPN is not connected. Check with `ssh <host> true`.

**Refusing to sync with --delete**
The `remote` path is empty, `/`, or a home directory. Point it at a dedicated project directory.

**File is outside repo**
The file path passed to `xsync` is not inside the project root. `xsync` only syncs files within the project.

**Exit code 23 from rsync**
Some files could not be transferred (permissions, vanished files). If the errors say "Operation not permitted" for `mkstemp` or "failed to set times", the remote directory is probably a network share: set `rsync_args` as shown in the configuration example.

## License

[MIT](LICENSE) © Robert Spaans
