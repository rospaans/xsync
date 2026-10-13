"""Paths typed or dragged in by the user, and the list of known projects."""

from pathlib import Path
import tomllib

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
