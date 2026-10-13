"""The command line: --help, --version, --init, and refusing everything else."""

import tomllib

import pytest

import xsync
from xsync import config as config_module
from helpers import run_xsync, tree, write_config


def test_help_and_version(tmp_path):
    result = run_xsync(tmp_path, "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert run_xsync(tmp_path, "-h").stdout == result.stdout
    assert run_xsync(tmp_path, "--version").stdout.strip() == f"xsync {xsync.__version__}"


def test_init_creates_a_valid_starter_config(tmp_path):
    result = run_xsync(tmp_path, "--init")
    assert result.returncode == 0, result.stdout + result.stderr
    config = tomllib.loads((tmp_path / ".xsync.toml").read_text())
    assert set(config) == {"remote", "exclude"}
    assert config["remote"] == "myhost:/path/to/project/"
    assert ".xsync.toml" in config["exclude"]
    assert run_xsync(tmp_path, "--init").returncode == 1  # never overwrites


def test_starter_config_optional_lines_are_valid_when_uncommented():
    lines = []
    for line in config_module.STARTER_CONFIG.splitlines():
        if line.startswith(("# interval =", "# rsync_args =")):
            line = line[2:]
        lines.append(line)
    config = tomllib.loads("\n".join(lines))
    assert config["interval"] == 2
    assert "--checksum" in config["rsync_args"]


@pytest.mark.parametrize("args", [
    ["--sync"], ["--dry-run"], ["--watch"], ["--status"], ["--interval", "5"],
    ["a.txt"], ["--dryrun"], ["-n"], ["--verbose", "a.txt"],
])
def test_removed_and_unknown_arguments_are_errors_and_never_sync(project, args):
    write_config(project.src, project.remote)
    result = run_xsync(project.src, *args)
    assert result.returncode == 2
    assert "unknown argument" in result.stdout
    assert tree(project.dst) == set()


@pytest.mark.parametrize("args", [[], ["--verbose"]])
def test_without_a_terminal_nothing_is_synced(project, args):
    write_config(project.src, project.remote)
    result = run_xsync(project.src, *args)
    assert result.returncode == 1
    assert "needs a terminal" in result.stdout
    assert tree(project.dst) == set()
