"""Gitignore-style matching for .tokensaverignore + .gitignore (simplified subset).

Semantics (aligned with gitignore where it matters):
- Trailing `/` marks a directory pattern; it excludes the dir and everything under it.
- A pattern containing `/` (after stripping the trailing one) is anchored to the
  workspace root and matched against the full relative path.
- A pattern without `/` matches the basename at any depth.
- `*` globs via fnmatch; `#` comments; blank lines skipped.
- Negation (`!`) is not supported — Token Saver errs on the side of not indexing.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import DEFAULT_IGNORES, IGNORE_NAME


class IgnoreMatcher:
    def __init__(self, patterns: list[str]):
        # each rule: (pattern, is_dir, anchored)
        self.rules: list[tuple[str, bool, bool]] = []
        for raw in patterns:
            pat = raw.strip()
            if not pat or pat.startswith("#") or pat.startswith("!"):
                continue
            is_dir = pat.endswith("/")
            pat = pat.rstrip("/")
            anchored = pat.startswith("/")
            pat = pat.lstrip("/")
            if "/" in pat:  # gitignore: a slash anywhere anchors to root
                anchored = True
            if pat:
                self.rules.append((pat, is_dir, anchored))

    def _dir_matches(self, rel_dir: str) -> bool:
        """True if rel_dir (posix, no leading slash) IS an ignored directory."""
        base = rel_dir.rsplit("/", 1)[-1]
        for pat, _is_dir, anchored in self.rules:
            if anchored:
                if fnmatch.fnmatch(rel_dir, pat):
                    return True
            elif fnmatch.fnmatch(base, pat):
                return True
        return False

    def matches_dir(self, rel_dir: str) -> bool:
        """True if rel_dir or any of its ancestors is ignored."""
        if not rel_dir or rel_dir == ".":
            return False
        parts = rel_dir.split("/")
        for i in range(1, len(parts) + 1):
            if self._dir_matches("/".join(parts[:i])):
                return True
        return False

    def matches_file(self, rel_path: str) -> bool:
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        if parent and self.matches_dir(parent):
            return True
        name = rel_path.rsplit("/", 1)[-1]
        for pat, is_dir, anchored in self.rules:
            if is_dir:
                continue  # dir rules handled via matches_dir on the parent
            if anchored:
                if fnmatch.fnmatch(rel_path, pat):
                    return True
            elif fnmatch.fnmatch(name, pat):
                return True
        return False


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
