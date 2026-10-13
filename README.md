# xsync

[![CI](https://github.com/rospaans/xsync/actions/workflows/ci.yml/badge.svg)](https://github.com/rospaans/xsync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A small `rsync` wrapper for syncing a local project directory to a server over SSH. One command, one config file per project.

## Recent changes: hardened for SSH remotes

SSH remotes (`host:/path/`, with `host` an alias from `~/.ssh/config`) are now the default setup. Local paths and mounted volumes still work; see "Optional: local paths and mounted volumes".

- **No remote shell commands**: `--watch` deletes remote files through rsync (`--delete-missing-args`) instead of `ssh host rm ...`, and the reachability check also goes through rsync. Odd filenames cannot inject commands, and xsync works with an SSH key restricted to rsync (see "Restricting xsync to one remote folder"). Watch-mode deletes over SSH need GNU rsync 3.1+ on both sides.
- **Stays inside the remote folder**: paths that are absolute or contain `..` are never synced or deleted, and a `remote` containing `..` is refused.
- **Single-file sync creates missing folders**: `xsync path/to/new/file.py` works even if `path/to/new/` does not exist on the remote yet.
- **`--delete` guard**: full syncs refuse to run when the remote path is empty, `/`, `~`, `.` or a home directory. A missing trailing `/` on `remote` is added automatically.
- **Compression only over SSH**: `-z` is dropped for local and mounted paths.
- **Fail fast**: an unreachable SSH host (down, key not loaded, VPN off) is reported within 5 seconds, before any sync and before `--watch` starts.
- **rsync flavor**: new optional `rsync = "/path/to/rsync"` key. Defaults to `/opt/homebrew/bin/rsync` if present, else `rsync` from `PATH`. A one-line warning is printed if it is not GNU rsync 3.x (recent macOS ships openrsync).
- **Extra rsync flags**: new optional `rsync_args = [...]` key, for remotes on storage that rejects permission, ownership or timestamp changes (see "Remotes on a network share").
- **Tests**: `python -m pytest tests`.

## What it does

`xsync` reads a `.xsync.toml` file from the project root and runs `rsync` over SSH against the `remote` target declared there. The logic lives in a single script on your `$PATH`; each project only needs a declarative config. It works from any terminal and any editor.

- Full sync: mirrors the project to the remote, respecting an exclude list.
- Single-file sync: pushes one file while preserving its relative path.
- Watch mode: keeps the remote up to date while you edit.
- Dry-run mode: preview what would change without transferring.

## Requirements

- Python 3.11+ (for `tomllib` in the standard library)
- `rsync`, preferably GNU rsync 3.x (`brew install rsync` on macOS, which ships openrsync)
- Key-based SSH login to the server through a host alias in `~/.ssh/config` (see "SSH setup")
- Optional: `watchdog` for `--watch` (installed by `install.sh`)

## Installation

```bash
git clone https://github.com/rospaans/xsync.git
cd xsync
bash install.sh
```

The installer copies `xsync` to `~/.local/bin` and, if needed, adds it to your `PATH` in `~/.zshrc` or `~/.bashrc`. Restart your shell or `source` the rc file afterwards.

## SSH setup

This is done once per server, before any project is configured. xsync never asks for a password. It needs a host alias in `~/.ssh/config` that logs in with a key.

```
Host myhost
    HostName server.example.org
    User you
    IdentityFile ~/.ssh/id_ed25519
    ControlMaster auto
    ControlPath ~/.ssh/cm-%n-%r-%p
    ControlPersist 10m
```

The three `Control*` lines are optional. They keep one connection open and reuse it, which makes repeated syncs and `--watch` much faster. Keep `%n` (the alias name) in `ControlPath`, so that two aliases for the same server never share a connection.

Check that it works without a prompt:

```bash
rsync --list-only myhost:.
```

The alias (`myhost`) is what each project's `remote` line will refer to.

## Restricting xsync to one remote folder

Optional but recommended on shared storage. Like the SSH setup, this is done once, before configuring projects.

xsync only addresses paths below `remote`. But the SSH login it uses can normally reach everything your account can, including other people's folders on shared storage. A wrong `remote` or a bug could therefore damage files outside your project.

You can rule that out on the server with a dedicated SSH key that is only allowed to run rsync inside one folder. Connections with that key cannot see or change anything outside it, whatever xsync or its config says. This uses `rrsync`, a script that ships with rsync.

Pick the folder that should be the boundary. A good choice is your own top-level folder on the storage: every project below it can then share one key, and the worst a mistake can do is affect your own files.

### One-time setup

The steps use these example values. Replace them with your own wherever they appear:

| Example | Meaning |
|---|---|
| `myhost` | your existing SSH alias for the server |
| `myhost-sync` | the new, restricted alias you are creating |
| `server.example.org`, `you` | the server's address and your username there |
| `/data/projects/you` | the boundary folder on the server |

Every command says where to run it: on **your machine** or on **the server**.

**Step 1. Find `rrsync` on the server.** On your machine:

```bash
ssh -T myhost 'command -v rrsync'
```

It prints a path, usually `/usr/bin/rrsync`. The steps below assume that path. If it prints nothing, ask the administrator to install it (it is part of the rsync package).

**Step 2. Create a dedicated key.** On your machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/xsync_ed25519 -C xsync
```

Press Enter twice to leave the passphrase empty, so xsync can use the key without a prompt.

**Step 3. Allow that key on the server, limited to the folder.** The server needs one new line in `~/.ssh/authorized_keys`: the restriction, followed by your new public key. You do not have to assemble it by hand. On your machine, this command builds the line and appends it on the server (change the folder path first):

```bash
echo "command=\"/usr/bin/rrsync /data/projects/you\",restrict $(cat ~/.ssh/xsync_ed25519.pub)" | ssh -T myhost 'cat >> ~/.ssh/authorized_keys'
```

Run it once only; running it again adds the line twice. Then check that it arrived:

```bash
ssh -T myhost 'tail -n 1 ~/.ssh/authorized_keys'
```

You should see a single line like this:

```
command="/usr/bin/rrsync /data/projects/you",restrict ssh-ed25519 AAAAC3Nza...long-key... xsync
```

**Step 4. Add the restricted alias.** On your machine, open `~/.ssh/config` and add this block at the end:

```
Host myhost-sync
    HostName server.example.org
    User you
    IdentityFile ~/.ssh/xsync_ed25519
    IdentitiesOnly yes
```

Use the same `HostName` and `User` as your existing `myhost` entry. Keep that entry as it is; it stays your alias for normal logins.

If you use connection sharing (`ControlMaster`), make sure the `ControlPath` contains `%n`, as in "SSH setup". With a path based only on user and server, the restricted alias would reuse an open connection from your normal alias and inherit its unrestricted access.

Then check whether the file has a `Host *` block containing an `IdentityFile` line. If it does, change `Host *` to `Host * !myhost-sync`. Without this, SSH offers your normal key first, the server accepts it, and the restriction is silently skipped. A complete file would look like this:

```
Host * !myhost-sync
    IdentityFile ~/.ssh/id_ed25519

Host myhost
    HostName server.example.org
    User you

Host myhost-sync
    HostName server.example.org
    User you
    IdentityFile ~/.ssh/xsync_ed25519
    IdentitiesOnly yes
```

**Step 5. Verify the restriction.** On your machine, run these three commands and compare the results:

```bash
rsync --list-only myhost-sync:.
```

Lists the contents of your boundary folder. The key works.

```bash
ssh myhost-sync
```

Must be refused, with a message like `rrsync error: Not invoked via sshd`. This key cannot open a shell, so you cannot log in and look around with it. If you get a normal shell instead, SSH is using your unrestricted key: go back to the `Host *` change in step 4.

```bash
rsync --list-only myhost-sync:../
```

Must be refused, with a message like `rrsync error: do not use .. in arg`. Nothing above the boundary folder is reachable.

**Step 6. Use the restricted alias in each project.** There are now two levels, each set in one place:

| Level | Set where | Example |
|---|---|---|
| The boundary: the outer limit for everything xsync does | Once, in the `authorized_keys` line on the server (step 3) | `/data/projects/you` |
| The project folder inside the boundary | Per project, in the `remote` line of its `.xsync.toml` | `myhost-sync:research/project_1/` |

In `remote`, use the restricted alias and write the path relative to the boundary folder, not as a full path:

```toml
remote = "myhost-sync:research/project_1/"
```

This syncs to `/data/projects/you/research/project_1/` on the server. For several projects:

| Local folder (contains `.xsync.toml`) | `remote` in that file | Ends up on the server at |
|---|---|---|
| `~/work/project_1/` | `myhost-sync:research/project_1/` | `/data/projects/you/research/project_1/` |
| `~/work/project_2/` | `myhost-sync:research/project_2/` | `/data/projects/you/research/project_2/` |

The key keeps every project inside the boundary. The `remote` line keeps each project in its own folder. Run `xsync --dry-run` first and check the destination on its first line.

### Adding another folder

A key is tied to exactly one folder. To sync into a second, unrelated folder, repeat steps 2 to 5 with a new key file name (for example `~/.ssh/xsync_other_ed25519`), the other folder's path and a new alias name. Add the new alias to the `Host *` exclusion as well (`Host * !myhost-sync !myhost-other`). The server's `authorized_keys` then has two lines:

```
command="rrsync /data/projects/you",restrict ssh-ed25519 AAAA... xsync
command="rrsync /data/other/folder",restrict ssh-ed25519 AAAA... xsync-other
```

Each project then chooses its boundary through the alias in its `remote`. Do not widen one key to a parent folder that also contains other people's files; that removes the protection.

### Requirements for this setup

- GNU rsync 3.1 or newer on both machines, because `--watch` removes deleted files through rsync rather than a shell command.
- New projects are created inside the boundary folder automatically, but their parent folder (`research/` in the example) must already exist.

## Configuration

Each project is configured by one file, `.xsync.toml`, in the project root. The next section shows how to create it; this is what it contains:

```toml
# Remote destination: SSH target, where the host is an alias from ~/.ssh/config
remote = "myhost:/path/to/project/"

# rsync binary to use (default: /opt/homebrew/bin/rsync if present, else rsync from PATH)
# rsync = "/opt/homebrew/bin/rsync"

# Extra rsync flags, e.g. when the remote directory is a network share that
# rejects permission, ownership or timestamp changes
# rsync_args = ["--no-perms", "--no-owner", "--no-group", "--no-times", "--omit-dir-times", "--inplace", "--checksum"]

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

The `remote` field is an SSH target in the form `host:/path/`, where `host` is an alias from `~/.ssh/config`. `user@host:/path/` also works.

Full syncs delete remote files that do not exist locally, so point `remote` at a folder used only for this project, and run `xsync --dry-run` before the first real sync. Files matching `exclude` are never deleted on the remote.

## Setting up a new project

This assumes the SSH alias exists (see "SSH setup").

Nothing is mounted. xsync copies one local folder to one folder on the server, and each side is specified in its own place:

| What | Where it is specified |
|---|---|
| The local folder to sync | Not written down. It is the folder that contains `.xsync.toml`. |
| The folder on the server | The `remote` line in that `.xsync.toml`. |
| Which files to skip | The `exclude` list in that `.xsync.toml`. |

Each project has its own `.xsync.toml`, so each project syncs to its own folder on the server and nowhere else.

**Step 1. Create the config.** In the root of your local project, generate a starter `.xsync.toml` (or copy `.xsync.toml.example` to `.xsync.toml`):

```bash
cd /path/to/my_project
xsync --init
```

**Step 2. Set the destination.** Open the new `.xsync.toml` and edit the `remote` line: the SSH alias, a colon, then the folder on the server, ending in `/`.

```toml
remote = "myhost:/data/projects/you/my_project/"
```

Use a folder that belongs to this project alone. A full sync deletes anything in it that does not exist locally (except files matching `exclude`). The folder itself is created on the first sync if its parent exists.

**Step 3. Adjust `exclude`.** Add folders that should stay only on one side, such as large data or results produced on the server.

**Step 4. If the server folder is on a network share,** add the `rsync_args` line from "Remotes on a network share".

**Step 5. Preview, then sync.**

```bash
xsync --dry-run        # lists what would be copied or deleted; changes nothing
xsync                  # first real sync
```

Check the destination printed on the first line of the dry run before running the real sync.

**Several projects** each get their own config and their own server folder:

| Local folder (contains `.xsync.toml`) | `remote` in that file |
|---|---|
| `~/work/project_1/` | `myhost:/data/projects/you/project_1/` |
| `~/work/project_2/` | `myhost:/data/projects/you/project_2/` |

Add `.xsync.toml` to the project's `.gitignore` if the remote path is personal, or commit it if it is generic enough to share.

If you use a restricted key (see "Restricting xsync to one remote folder"), `remote` uses the restricted alias and a path relative to the boundary folder instead: `myhost-sync:my_project/`.

## Remotes on a network share

If the folder on the server is itself a network share that the server has mounted (CIFS/SMB, some NFS setups), the server may refuse to change permissions, ownership or timestamps. rsync then reports errors like these and exits with code 23, even though file contents arrive:

```
rsync: failed to set times on "...": Operation not permitted (1)
rsync: mkstemp "..." failed: Operation not permitted (1)
```

Tell rsync not to attempt those changes by adding `rsync_args` to `.xsync.toml`:

```toml
rsync_args = ["--no-perms", "--no-owner", "--no-group", "--no-times", "--omit-dir-times", "--inplace", "--checksum"]
```

| Flag | Effect |
|---|---|
| `--no-perms`, `--no-owner`, `--no-group` | Do not set permissions or ownership. |
| `--no-times`, `--omit-dir-times` | Do not set modification times on files and folders. |
| `--inplace` | Write files directly instead of through a temporary file. |
| `--checksum` | Detect changes by content, because modification times no longer match. |

`--checksum` makes a full sync read every file on both sides. That is quick for code, but slow if large data files are not excluded. Single-file sync and `--watch` are not affected.

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

## How it works

- **Full sync** (`xsync`): invokes `rsync -avz --delete` over SSH with the exclude list. The remote becomes an exact mirror of the local project, minus excluded paths.
- **Single-file sync** (`xsync path/to/file`): transfers just that file, preserving its relative path under the remote root and creating missing folders.
- **Watch** (`xsync --watch`): one full sync, then pushes changed files in batches and removes deleted files from the remote.

## Optional: local paths and mounted volumes

SSH is the recommended setup. `remote` can also be a path on your own machine, such as a mounted network volume or an external drive:

```toml
remote = "/Volumes/your-volume/your-project/"
```

`~/some/folder/` works too. Differences from an SSH remote:

- No SSH alias or key is needed, and no reachability check runs. If the volume is not mounted, rsync reports the error.
- Compression (`-z`) is switched off, since nothing travels over a network connection that rsync controls.
- `--watch` removes deleted files directly on the mounted path.
- The same `--delete` guard applies: the path must not be `/`, your home directory or the current directory.
- A mounted share can reject permission or timestamp changes in the same way as a server-side one. If you see "Operation not permitted", use the `rsync_args` from "Remotes on a network share".

## Troubleshooting

**No `.xsync.toml` found**
Either `cd` into a project that has one, or run `xsync --init` to create one here.

**Cannot reach host over SSH**
The host is down, your key is not loaded (`ssh-add`), or the VPN is not connected. Check with `rsync --list-only <host>:.`.

**Refusing to sync with --delete**
The `remote` path is empty, `/`, or a home directory. Point it at a dedicated project directory.

**File is outside repo**
The file path passed to `xsync` is not inside the project root. `xsync` only syncs files within the project.

**Exit code 23 from rsync**
Some files or attributes could not be transferred. If the errors say "Operation not permitted", see "Remotes on a network share".

**Works with my normal alias but not with the restricted one**
Check that `remote` is relative to the boundary folder (`myhost-sync:research/project_1/`, not an absolute path), that the parent folder exists, and that `rsync --list-only myhost-sync:.` works.

**rsync warning about "not GNU rsync 3.x"**
macOS ships a different rsync. Run `brew install rsync`; xsync picks up `/opt/homebrew/bin/rsync` automatically.

## License

[MIT](LICENSE) © Robert Spaans
