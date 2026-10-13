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
- **rsync flavor**: xsync uses `/opt/homebrew/bin/rsync` if present, else `rsync` from `PATH`, and prints a one-line warning if it is not GNU rsync 3.x (recent macOS ships openrsync). An optional `rsync = "/path/to/rsync"` key overrides the choice (see Troubleshooting).
- **Extra rsync flags**: new optional `rsync_args = [...]` key, for example for server folders on storage that rejects permission, ownership or timestamp changes (see "Remotes on a network share").
- **Readable output**: a header naming the mode, one line per new, updated or deleted file, and a short summary, instead of rsync's raw output (`--verbose` brings that back). In `--watch` the header stays fixed at the top with a live status.
- **Interactive session**: plain `xsync` opens a screen with a status bar and single-key actions: sync, watch on/off, compare, sync one file or folder, reconnect, errors. `xsync --sync` is the one-off full sync.
- **Watch survives connection drops**: changes are queued and retried until the server is reachable again, and emptied folders are removed from the remote.
- **`xsync --status`** shows the resolved setup and checks the host without syncing; `xsync --version` prints the version.
- **Tests**: `python -m pytest tests`.

## What it does

`xsync` reads a `.xsync.toml` file from the project root and runs `rsync` over SSH against the `remote` target declared there. The logic lives in a single script on your `$PATH`; each project only needs a declarative config. It works from any terminal and any editor.

- Full sync: mirrors the project to the remote, respecting an exclude list.
- File or folder sync: pushes one file, or mirrors one folder, while preserving its relative path.
- Compare: shows what differs between the project and the server without changing anything.
- An interactive session that offers all of these on single keys.
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
# Where to sync to: "<ssh alias>:<folder on the server>/"
remote = "myhost:/path/to/project/"

# Files and folders to skip (rsync exclude syntax)
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

# --- Optional settings: remove the leading "# " from a line to use it ---
# A local path instead of SSH (replaces the remote line above)
# remote = "/Volumes/your-volume/your-project/"

# Extra rsync flags. This set is for a server folder on a network share (CIFS/SMB)
# that rejects permission, ownership and timestamp changes; see the README.
# rsync_args = ["--no-perms", "--no-owner", "--no-group", "--no-times", "--omit-dir-times", "--inplace", "--checksum"]
```

| Setting | Required | Meaning |
|---|---|---|
| `remote` | yes | Where to sync to. |
| `exclude` | no | Files and folders to skip. |
| `rsync_args` | no | A list of extra flags passed to rsync, placed after xsync's own so they can override them. |

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

**Step 4. If the server folder is on a network share,** you may need an `rsync_args` line. See "Remotes on a network share".

**Step 5. Preview, then sync.**

```bash
xsync --dry-run        # lists what would be copied or deleted; changes nothing
xsync --sync           # first real sync
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

xsync has no built-in setting for this, because what a share refuses differs from one to the next. Use `rsync_args` in `.xsync.toml` to switch off only what yours rejects. Match each error to a flag:

| Error mentions | Add to `rsync_args` | Effect |
|---|---|---|
| `failed to set permissions` | `--no-perms` | Do not set permissions. |
| `chown` or `chgrp` | `--no-owner`, `--no-group` | Do not set ownership. |
| `failed to set times` on a folder | `--omit-dir-times` | Do not set modification times on folders. |
| `failed to set times` on a file | `--no-times`, `--checksum` | Do not set modification times on files, and detect changes by content instead. |
| `mkstemp ... failed` | `--inplace` | Write files directly instead of through a temporary file. |

Add flags until `xsync` runs without errors. As an example, a share that refuses all of the above needs:

```toml
rsync_args = ["--no-perms", "--no-owner", "--no-group", "--no-times", "--omit-dir-times", "--inplace", "--checksum"]
```

`--checksum` makes a full sync read every file on both sides. That is quick for code, but slow if large data files are not excluded. Single-file sync and `--watch` are not affected.

## Usage

```bash
xsync                      # open the session: compare, then sync or watch with single keys
xsync --sync               # sync the entire project once and exit
xsync path/to/file.py      # sync a single file or folder
xsync --dry-run            # show what would sync, no transfer
xsync file.py --dry-run    # dry-run for a single file or folder
xsync --watch              # open the session with watch mode already on
xsync --watch --interval 5 # watch with a 5-second debounce window
xsync --init               # create a starter .xsync.toml in current dir
xsync --status             # show the resolved setup and check the host; no sync
xsync --verbose            # show rsync's raw output instead of the summary
xsync --version            # show the version
xsync --help               # show usage
```

`xsync` walks up from the current directory to find `.xsync.toml`, so you can run it from any subdirectory of the project. An unknown option is an error; xsync never falls back to a sync on a typo.

### The session

Running `xsync` in a terminal opens a session. It first compares the project with the server, like `git status`, and changes nothing until you press a key:

```
 XSYNC   my_project  →   SSH  myhost:/data/projects/you/my_project/                        ● 3 differences at 15:52    0 changes 

  s  Sync    w  Watch off    c  Compare    f  Path    r  Reconnect    i  Info    e  Errors    q  Quit

──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
15:52:10  ~ src/model.py  (not synced)
15:52:10  + notes/plan.md  (not synced)
15:52:10  - old/unused.py  (not synced)
15:52:10  3 difference(s), nothing changed; press s to sync
```

The top of the screen stays fixed; changes scroll below it. Each key is drawn as a button. The `w` button turns green while watch mode is on, and the `r` button turns magenta when the connection is lost.

| Key | Action |
|---|---|
| `s` | Sync the whole project now. |
| `w` | Switch watch mode on or off. Switching it on does a full sync first, then sends changes as you make them. |
| `c` | Compare: list what differs between the project and the server, without changing anything. |
| `f` | Sync one file or folder. Type, paste or drag a path into the terminal and press Enter (Esc cancels). A folder is mirrored like a full sync, limited to that folder. |
| `r` | Check the connection now and, when it is back, send what was queued. |
| `i` | Info: show the resolved setup (project, remote, whether the host answers, rsync and flags in use), the same as `xsync --status`. |
| `e` | List the warnings and errors recorded so far, and clear the problem line. |
| `q` | Quit. Ctrl+C works too. |

- **The bar** shows the mode (`XSYNC` when idle, `WATCH`, or `OFFLINE`), what is synced where with an `SSH` or `LOCAL` tag in front of the remote, and on the right the current state behind a dot: green when in sync, yellow when busy or when there are differences, magenta for problems.
- **The line under the buttons** is empty when all is well. It shows the most recent warning or error until you press `e`, and the input prompt after `f`.
- **The terminal tab title** shows the state too, so you can see it when the terminal is in the background.
- **If the connection drops**, the session keeps running. It checks the server once a minute even when nothing changes; when offline, the bar says so, changes made in watch mode are queued, and xsync retries every 10 seconds.
- **In watch mode**, deleted files are removed from the remote. A folder that disappears locally is removed too, but only once it is empty on the remote, so files that exist only on the server are never touched.

The session needs a terminal and GNU rsync 3.x. When the output is piped or redirected, `xsync` syncs once and exits, and `xsync --watch` runs as plain watch mode without keys.

### Output of one-off commands

Commands that run once and exit (`--sync`, `--dry-run`, a file or folder) print a header that says which mode is running, what is synced where, and how:

```
xsync sync     my_project → myhost:/data/projects/you/my_project/  ssh
xsync dry-run  my_project → myhost:/data/projects/you/my_project/  ssh
xsync file     my_project → myhost:/data/projects/you/my_project/  ssh
```

In a terminal the mode is shown as a colored badge. Below the header, each change is one line:

```
xsync sync  my_project → myhost:/data/projects/you/my_project/  ssh
  + src/new_module.py
  ~ README.md
  - old/unused.py
  ! rsync: [generator] failed to set times on "...": Operation not permitted (1)
1 new, 1 updated, 1 deleted · 0.8s
```

| Mark | Color | Meaning |
|---|---|---|
| `+` | green | New on the remote. |
| `~` | blue | Updated on the remote. |
| `-` | red | Deleted from the remote. |
| `!` | yellow | A message or warning from rsync or SSH, shown as received. |
| `!` | magenta | A failure reported by xsync, such as a lost connection or rsync's exit code. |

The last line is a summary, or "Already up to date". With `--dry-run` it says "would be".

### Checking the setup

`xsync --status` syncs nothing. It shows what xsync would use and whether the server answers:

```
xsync status  my_project → myhost:/data/projects/you/my_project/
  project     /home/you/work/my_project
  config      /home/you/work/my_project/.xsync.toml
  remote      myhost:/data/projects/you/my_project/  (ssh)
  host        reachable
  full sync   allowed
  rsync       /opt/homebrew/bin/rsync  (GNU rsync 3.5.1)
  rsync_args  none
  exclude     16 patterns
  watch mode  available
```

Colors and the fixed header are used in a terminal and left out when the output is piped; `NO_COLOR` switches colors off. The summarized output needs GNU rsync 3.x; with another rsync, or with `--verbose`, you get rsync's own output.

## How it works

- **Full sync** (`xsync --sync`, or `s` in the session): invokes `rsync -avz --delete` over SSH with the exclude list. The remote becomes an exact mirror of the local project, minus excluded paths.
- **File or folder sync** (`xsync path/to/file`, or `f` in the session): a file is transferred to the same relative path under the remote root, creating missing folders. A folder is mirrored like a full sync, limited to that folder.
- **Watch** (`xsync --watch`): one full sync, then pushes changed files in batches and removes deleted files from the remote. Failed batches are retried when the connection returns.
- **SSH options**: over SSH, xsync runs rsync with `ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3`, so it never waits for a password and notices a dead connection. If you set `RSYNC_RSH`, or pass your own `-e` in `rsync_args`, yours is used instead.

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
- A mounted share can reject permission or timestamp changes in the same way as a server-side one. If you see "Operation not permitted", see "Remotes on a network share".

## Troubleshooting

**"no .xsync.toml found"**
Either `cd` into a project that has one, or run `xsync --init` to create one here.

**"cannot reach '<host>' over SSH"**
The host is down, your key is not loaded (`ssh-add`), or the VPN is not connected. Check with `rsync --list-only <host>:.`.

**"refusing to sync with --delete"**
The `remote` path is empty, `/`, or a home directory. Point it at a dedicated project directory.

**"not found inside the project"**
The file or folder passed to `xsync` (or entered with `f`) does not exist, or is not inside the project root. `xsync` only syncs paths within the project.

**Something is off and I don't know where to start**
Run `xsync --status`. It shows the project, remote, rsync binary and flags in use, and whether the server answers.

**I need to see exactly what rsync did**
Run the same command with `--verbose` to get rsync's raw output.

**Exit code 23 from rsync**
Some files or attributes could not be transferred. If the errors say "Operation not permitted", see "Remotes on a network share".

**Works with my normal alias but not with the restricted one**
Check that `remote` is relative to the boundary folder (`myhost-sync:research/project_1/`, not an absolute path), that the parent folder exists, and that `rsync --list-only myhost-sync:.` works.

**rsync warning about "not GNU rsync 3.x"**
macOS ships a different rsync. Run `brew install rsync`; xsync picks up `/opt/homebrew/bin/rsync` automatically.

**xsync uses the wrong rsync**
xsync runs `/opt/homebrew/bin/rsync` if it exists, and otherwise the first `rsync` on your `PATH`. If your GNU rsync is installed somewhere else (for example `/usr/local/bin/rsync` on an Intel Mac), the warning above keeps appearing or flags are rejected. Check what you have:

```bash
which -a rsync                  # every rsync on your PATH
/path/to/rsync --version        # GNU rsync prints "rsync  version 3.x"
```

Then point xsync at the right one by adding this line to the project's `.xsync.toml`:

```toml
rsync = "/usr/local/bin/rsync"
```

This setting is not in the starter config because the automatic choice is right in most setups.

## License

[MIT](LICENSE) © Robert Spaans
