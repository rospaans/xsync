"""Telling SSH targets from local paths, and refusing remotes a --delete sync must never touch."""

import pytest

from xsync import remote as remote_module
from helpers import HOME


@pytest.mark.parametrize("remote, expected", [
    ("/Volumes/vol/project/", False),
    ("~/project/", False),
    ("./project/", False),
    ("host:/path", True),
    ("user@host:/path", True),
])
def test_is_ssh_remote(remote, expected):
    assert remote_module.is_ssh_remote(remote) is expected


@pytest.mark.parametrize("remote", [
    "",
    "/",
    "~",
    "~/",
    ".",
    "./",
    HOME,
    HOME + "/",
    "host:",
    "host:/",
    "host:~",
    "host:~/",
    "host:.",
    "user@host:/",
    f"host:{HOME}/",
    "host:/srv/project/../",
    "host:project/../../other/",
    "/Volumes/vol/project/../other/",
])
def test_dangerous_remotes_are_rejected(remote):
    assert remote_module.unsafe_remote_reason(remote) is not None


@pytest.mark.parametrize("remote", [
    "host:/srv/project/",
    "host:project/",
    "user@host:~/project/",
    "/Volumes/vol/project/",
    "~/project/",
    HOME + "/project",
])
def test_normal_remotes_pass(remote):
    assert remote_module.unsafe_remote_reason(remote) is None


def test_normalize_remote():
    assert remote_module.normalize_remote("host:/srv/project") == "host:/srv/project/"
    assert remote_module.normalize_remote("host:~/project/") == "host:~/project/"
    assert remote_module.normalize_remote("/Volumes/vol/project") == "/Volumes/vol/project/"
    assert remote_module.normalize_remote("~/project") == HOME + "/project/"
