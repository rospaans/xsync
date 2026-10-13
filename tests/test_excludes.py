"""The exclude check that watch mode uses, which has to agree with rsync's own patterns."""

from pathlib import Path

import pytest

from xsync import excludes as excludes_module


@pytest.mark.parametrize("path, expected", [
    ("/repo/main.py", False),
    ("/repo/src/main.py", False),
    # file patterns
    ("/repo/main.pyc", True),
    ("/repo/src/main.pyc", True),
    ("/repo/.DS_Store", True),
    # directory patterns
    ("/repo/.git/config", True),
    ("/repo/data/table.csv", True),
    # nested directories
    ("/repo/src/data/deep/table.csv", True),
    ("/repo/src/pkg/__pycache__/x.py", True),
    # internal files
    ("/repo/.xsync.files", True),
    ("/repo/.xsync.exclude", True),
    # outside the repo
    ("/elsewhere/main.py", True),
])
def test_make_excluded_checker(path, expected):
    patterns = [".git/", "data/", "__pycache__/", "*.pyc", ".DS_Store"]
    is_excluded = excludes_module.make_excluded_checker(Path("/repo"), patterns)
    assert is_excluded(path) is expected


@pytest.mark.parametrize("path, expected", [
    # "/data/" is anchored: only the top-level data folder
    ("/repo/data/table.csv", True),
    ("/repo/data/raw/deep/table.csv", True),
    ("/repo/src/pkg/data/loader.py", False),
    ("/repo/data.py", False),
    # multi-part directory pattern
    ("/repo/scripts/experiments/soma/README.md", True),
    ("/repo/scripts/experiments/soma/src/model.py", True),
    ("/repo/scripts/experiments/other/run.py", False),
    ("/repo/scripts/soma.py", False),
    # anchored file pattern
    ("/repo/notes.txt", True),
    ("/repo/docs/notes.txt", False),
    # a directory pattern does not match a file of the same name
    ("/repo/src/outputs", False),
    ("/repo/src/outputs/run1.json", True),
])
def test_excluded_checker_anchored_and_nested_patterns(path, expected):
    patterns = ["/data/", "scripts/experiments/soma/", "/notes.txt", "outputs/"]
    is_excluded = excludes_module.make_excluded_checker(Path("/repo"), patterns)
    assert is_excluded(path) is expected
