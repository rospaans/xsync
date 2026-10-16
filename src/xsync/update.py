"""Newer versions on GitHub: finding out about them, and installing one from the user's checkout."""

import json
import os
import re
import subprocess
import urllib.request
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import __version__

# The version on the main branch is whatever its __init__.py says
LATEST_URL = "https://raw.githubusercontent.com/rospaans/xsync/main/src/xsync/__init__.py"
TIMEOUT_SECONDS = 5
# Set this (to anything) to never check at startup, whatever the settings say
OPT_OUT = "XSYNC_NO_UPDATE_CHECK"
BRANCH = "main"
# Seconds git pull and install.sh together may take
UPDATE_TIMEOUT_SECONDS = 300


def version_tuple(version: str) -> tuple[int, ...]:
    """'0.10.2' as (0, 10, 2), so versions compare as numbers; anything after the digits is ignored."""
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0].split("-")[0]))


def is_newer(latest: str | None) -> bool:
    return latest is not None and version_tuple(latest) > version_tuple(__version__)


def latest_version() -> str | None:
    """The version on GitHub's main branch, or None if it cannot be fetched."""
    try:
        with urllib.request.urlopen(LATEST_URL, timeout=TIMEOUT_SECONDS) as response:
            text = response.read(10_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    found = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return found.group(1) if found else None


def install_folder() -> str | None:
    """The checkout xsync was installed from (install.sh installs from one), if pip recorded it."""
    try:
        recorded = metadata.distribution("xsync").read_text("direct_url.json")
        url = json.loads(recorded or "{}").get("url", "")
    except (metadata.PackageNotFoundError, ValueError):
        return None
    parsed = urlparse(url)
    return unquote(parsed.path) if parsed.scheme == "file" else None


def update_command(folder: str | None) -> str:
    return f"cd {folder} && git pull && bash install.sh" if folder else "git pull in your xsync checkout, then bash install.sh"


def update_note(latest: str | None) -> str | None:
    """A line saying a newer version exists and how to get it; None if up to date or unknown."""
    if not is_newer(latest):
        return None
    return f"xsync {latest} is available (you have {__version__}). Press , for settings and u to update, or run: {update_command(install_folder())}"


def git(folder: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def why_not_update(folder: str | None) -> str | None:
    """Why updating from this checkout is not safe to do automatically; None if it is."""
    if folder is None:
        return f"xsync was not installed from a checkout, so it cannot update itself. Run: {update_command(None)}"
    if not (Path(folder) / "install.sh").is_file():
        return f"no install.sh in {folder}"
    try:
        branch = git(folder, "symbolic-ref", "--short", "HEAD")
    except OSError:
        return "git is not installed"
    if branch.returncode != 0:
        return f"{folder} is not a git checkout on a branch"
    if branch.stdout.strip() != BRANCH:
        return f"{folder} is on branch '{branch.stdout.strip()}', not '{BRANCH}'; update it yourself"
    if git(folder, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return f"{folder} has uncommitted changes; update it yourself"
    return None


def run_update(folder: str) -> tuple[bool, list[str]]:
    """git pull --ff-only, then install.sh, in the checkout. Returns whether it worked, and what they printed."""
    lines = []
    for step in (["git", "-C", folder, "pull", "--ff-only"], ["bash", str(Path(folder) / "install.sh")]):
        lines.append("$ " + " ".join(step))
        try:
            done = subprocess.run(step, cwd=folder, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  timeout=UPDATE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            lines.append(str(error))
            return False, lines
        lines += [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]
        if done.returncode != 0:
            return False, lines
    return True, lines


def check_disabled() -> bool:
    return bool(os.environ.get(OPT_OUT))
