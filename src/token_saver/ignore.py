"""Gitignore-style matching for .tokensaverignore + .gitignore (simplified subset).

Supports: trailing-slash dir patterns, `*` globs, leading `/` anchors, `#` comments.
Does not support negation (`!`) — Token Saver errs on the side of not indexing.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import DEFAULT_IGNORES, IGNORE_NAME


class IgnoreMatcher:
    def __init__(self, patterns: list[str]):
        self.dir_patterns: list[str] = []
        self.file_patterns: list[str] = []
        for raw in patterns:
            pat = raw.strip()
            if not pat or pat.startswith("#") or pat.startswith("!"):
                continue
            if pat.endswith("/"):
                self.dir_patterns.append(pat.rstrip("/").lstrip("/"))
            else:
                self.file_patterns.append(pat.lstrip("/"))

    def matches_dir(self, rel_parts: tuple[str, ...]) -> bool:
        return any(
            fnmatch.fnmatch(part, pat)
            for part in rel_parts
            for pat in self.dir_patterns
        )

    def matches_file(self, rel_path: str) -> bool:
        name = rel_path.rsplit("/", 1)[-1]
        for pat in self.file_patterns:
            if "/" in pat:
                if fnmatch.fnmatch(rel_path, pat):
                    return True
            elif fnmatch.fnmatch(name, pat):
                return True
        # a file under an ignored directory
        parts = tuple(rel_path.split("/")[:-1])
        return self.matches_dir(parts)


def load_matcher(root: Path) -> IgnoreMatcher:
    patterns = list(DEFAULT_IGNORES)
    for fname in (IGNORE_NAME, ".gitignore", ".claudeignore"):
        f = root / fname
        if f.exists():
            try:
                patterns.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
    return IgnoreMatcher(patterns)
