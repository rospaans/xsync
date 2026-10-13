"""The `remote` setting: telling SSH targets from local paths, and refusing dangerous ones."""

import os
from pathlib import Path


# No prompts, and notice a dead connection within about a minute instead of hanging
SSH_COMMAND = "ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"


def is_ssh_remote(remote: str) -> bool:
    if remote.startswith("/") or remote.startswith("~/") or remote.startswith("./"):
        return False
    return ":" in remote


def normalize_remote(remote: str) -> str:
    """Expand ~ for local remotes and make sure the remote ends with a slash."""
    if not is_ssh_remote(remote):
        remote = os.path.expanduser(remote)
    return remote if remote.endswith("/") else remote + "/"


def unsafe_remote_reason(remote: str) -> str | None:
    """Return why remote is too dangerous for a --delete sync, or None if it is fine."""
    path = remote.split(":", 1)[1] if is_ssh_remote(remote) else os.path.expanduser(remote)
    stripped = path.rstrip("/")
    if not stripped:
        return "the remote path is empty or the filesystem root"
    # "." is the home directory over SSH and the current directory locally
    if os.path.normpath(stripped) in ("~", ".", str(Path.home())):
        return "the remote path is a home (or the current) directory"
    if ".." in Path(stripped).parts:
        return "the remote path contains '..'"
    return None
