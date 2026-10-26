"""Paths typed or dragged in by the user, and the list of known projects."""

import os
import tempfile
import tomllib
from pathlib import Path

import pytest

from xsync import config as config_module


def test_resolve_in_project(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "proj"
    (root / "sub dir").mkdir(parents=True)
    (root / "sub dir" / "a file.py").write_text("x")
    (root / "top.py").write_text("x")
    outside = tmp_path.resolve() / "outside.txt"
    outside.write_text("x")
    monkeypatch.chdir(root / "sub dir")

    assert config_module.resolve_in_project("top.py", root) == "top.py"                      # relative to the root
    assert config_module.resolve_in_project("a file.py", root) == "sub dir/a file.py"        # relative to the cwd
    assert config_module.resolve_in_project(str(root / "top.py"), root) == "top.py"          # absolute
    assert config_module.resolve_in_project(f"'{root}/sub dir/a file.py'  ", root) == "sub dir/a file.py"  # quoted paste
    dragged = str(root / "sub dir" / "a file.py").replace(" ", "\\ ")
    assert config_module.resolve_in_project(dragged, root) == "sub dir/a file.py"            # dragged from Finder
    assert config_module.resolve_in_project("sub dir", root) == "sub dir"                    # a folder
    assert config_module.resolve_in_project(str(root), root) == "."
    assert config_module.resolve_in_project("missing.py", root) is None
    assert config_module.resolve_in_project(str(outside), root) is None
    assert config_module.resolve_in_project("../../outside.txt", root) is None
    assert config_module.resolve_in_project("   ", root) is None


def test_registry_round_trip(tmp_path):
    path = tmp_path / "config" / "xsync" / "projects.toml"
    assert config_module.load_registry(path) == []
    roots = [tmp_path / "one", tmp_path / 'with "quotes" and spaces']
    config_module.save_registry(path, roots)
    assert config_module.load_registry(path) == roots
    assert tomllib.loads(path.read_text())["projects"] == [str(root) for root in roots]
    path.write_text("not [valid toml")
    assert config_module.load_registry(path) == []


def test_registry_lives_in_the_user_config_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_module.registry_path() == tmp_path / "xsync" / "projects.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_module.registry_path() == Path.home() / ".config" / "xsync" / "projects.toml"


def test_only_one_xsync_can_hold_the_lock(tmp_path):
    path = tmp_path / "config" / "xsync" / "lock"
    first = config_module.acquire_lock(path)
    assert first is not None and path.read_text().strip() == str(os.getpid())
    assert config_module.acquire_lock(path) is None          # a second xsync is turned away
    first.close()
    second = config_module.acquire_lock(path)                # free again once the first has ended
    assert second is not None
    second.close()


def test_temp_files_are_created_outside_any_project(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    path = config_module.write_temp_file("exclude", ["*.pyc", "data/"])
    assert path.parent == tmp_path and path.name.startswith("xsync-") and path.suffix == ".exclude"
    assert path.read_text() == "*.pyc\ndata/\n"


def test_complete_path_works_like_a_shell(tmp_path, monkeypatch):
    for folder in ("projects/alpha", "projects/beta", "projects/bravo", "projects/.hidden", "my folder"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "projects" / "notes.txt").write_text("x")
    complete = lambda text, **options: config_module.complete_path(text, tmp_path, **options)

    assert complete("proj") == ("projects/", [])                         # one fit: completed, with a /
    assert complete("projects/a") == ("projects/alpha/", [])
    assert complete("projects/b") == ("projects/b", ["beta/", "bravo/"])  # several: as far as they agree
    assert complete("projects/") == ("projects/", ["alpha/", "beta/", "bravo/"])   # folders only, no hidden
    assert complete("projects/.") == ("projects/.hidden/", [])           # hidden once a . is typed
    assert complete("projects/n", folders_only=False) == ("projects/notes.txt", [])
    assert complete("projects/zzz") == ("projects/zzz", [])              # nothing fits: unchanged
    assert complete("my") == ("my folder/", [])                          # spaces are fine
    assert complete(str(tmp_path / "proj")) == (str(tmp_path / "projects") + "/", [])   # absolute
    monkeypatch.setenv("HOME", str(tmp_path))
    assert complete("~") == ("~/", [])
    assert complete("~/pro") == ("~/projects/", [])                      # ~ stays as typed
    assert complete("nowhere/x") == ("nowhere/x", [])


def test_a_folder_that_may_not_be_read_is_not_empty(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError) as raised:
            config_module.complete_path("locked/", tmp_path)
    finally:
        locked.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_module.cannot_read(raised.value).startswith("can't read ~/locked: permission denied")

def test_a_path_that_only_fits_from_home_is_suggested(tmp_path, monkeypatch):
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    (home / "Documents" / "papers").mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert config_module.from_home("Documents/", elsewhere) == "~/Documents/"
    assert config_module.from_home("Docu", elsewhere) == "~/Docu"
    assert config_module.from_home("Documents/papers", elsewhere) == "~/Documents/papers"
    assert config_module.from_home("Nothing/", elsewhere) is None
    assert config_module.from_home("~/Nothing", elsewhere) is None   # already from home
    assert config_module.from_home("Nothing", home) is None          # started in the home folder itself
