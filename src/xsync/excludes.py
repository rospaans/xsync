"""Decide whether a path is excluded, the way rsync's exclude patterns would."""

import fnmatch
from pathlib import Path

from .config import INTERNAL_FILES


def pattern_matches(pattern: str, parts: tuple[str, ...], is_dir: bool) -> bool:
    """Test one rsync-style exclude pattern against a path given as its components.

    Supports the common subset: a trailing / matches directories only, a leading /
    anchors at the project root, and a pattern containing / matches whole
    components at the end of the path.
    """
    if pattern.endswith("/") and not is_dir:
        return False
    anchored = pattern.startswith("/")
    pat = pattern.strip("/")
    if not pat:
        return False
    if anchored:
        return fnmatch.fnmatch("/".join(parts), pat)
    depth = pat.count("/") + 1
    return len(parts) >= depth and fnmatch.fnmatch("/".join(parts[-depth:]), pat)


def make_excluded_checker(repo_root: Path, patterns: list[str]):
    """Return a function that tests whether a resolved path should be excluded."""
    def is_excluded(abs_path: str) -> bool:
        try:
            rel = Path(abs_path).relative_to(repo_root)
        except ValueError:
            return True
        if rel.name in INTERNAL_FILES:
            return True
        parts = rel.parts
        for pattern in patterns:
            # The file itself, then every directory above it
            if pattern_matches(pattern, parts, is_dir=False):
                return True
            if any(pattern_matches(pattern, parts[:i], is_dir=True) for i in range(1, len(parts))):
                return True
        return False
    return is_excluded
