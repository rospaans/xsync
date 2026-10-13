"""
xsync: keep local projects in sync with a server over SSH, from one interactive screen.

Each project has a .xsync.toml in its root that says where it syncs to. The
remote is normally an SSH target, "host:/path/to/project/", where host is an
alias from ~/.ssh/config that logs in with a key. A local path (for example a
mounted volume) also works.

Usage:
    xsync                      # the overview of all projects. Nothing is compared, synced or
                               # contacted until you press a key.
    xsync --verbose            # the same, showing rsync's raw output
    xsync --init               # create a starter .xsync.toml in current dir
    xsync --version            # show the version
    xsync --help               # show this help

xsync remembers every project you run it in. The overview lists them all:
    ↑ ↓  move the highlight          a  add a folder (creates its .xsync.toml if missing)
    o    open the highlighted one    e  edit its .xsync.toml in your editor
    s w c  sync, watch on/off, compare it     x  forget it (no files are touched)

Keys inside a project:
    s  sync the whole project          r  check the connection, send what was queued
    w  watch mode on/off               i  info: the resolved setup
    c  compare, changing nothing       p  problems: recorded warnings and errors
    f  sync one file or folder         e  edit .xsync.toml in your editor
    b  back to the overview            l  clear the list
    ↑ ↓ PgUp PgDn Home End, or the mouse wheel: scroll back through the list
    v  verbose on/off                  h  help                  q  quit

Watch mode keeps running for every project it is switched on for, whichever
view is on screen.
"""

import sys
from pathlib import Path

from . import __version__
from .app import App
from .config import STARTER_CONFIG, find_root, registry_path


def cmd_init():
    """Scaffold a starter .xsync.toml in the current directory."""
    target = Path.cwd() / ".xsync.toml"
    if target.exists():
        print(f"Already exists: {target}")
        sys.exit(1)
    target.write_text(STARTER_CONFIG)
    print(f"Created {target}")
    print("Edit the 'remote' line in it to point to your destination.")


KNOWN_FLAGS = {"--init", "--verbose", "--version", "--help", "-h"}


def main():
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__.strip())
        return

    if "--version" in args:
        print(f"xsync {__version__}")
        return

    # Syncing only ever happens on a key press inside the session
    unknown = [a for a in args if a not in KNOWN_FLAGS]
    if unknown:
        print(f"error: unknown argument: {' '.join(unknown)}")
        print("   Run `xsync` and use the keys, or `xsync --help`.")
        sys.exit(2)

    if "--init" in args:
        cmd_init()
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: xsync needs a terminal. Run `xsync` in a terminal and use the keys.")
        sys.exit(1)

    app = App(registry_path(), verbose="--verbose" in args)
    # Always the overview. Run inside a project, that project is remembered and highlighted.
    here = find_root(Path.cwd())
    if here is not None:
        app.selected = app.projects.index(app.register(here))
    app.run()
