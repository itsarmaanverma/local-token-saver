"""Workspace resolution.

Resolution order (per product spec):
  1. Explicit path argument.
  2. Nearest ancestor of cwd containing .tokensaver/config.json.
  3. Nearest ancestor containing .git/.
  4. Nearest ancestor containing CLAUDE.md or AGENTS.md.
  5. cwd itself.
"""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG_NAME, TOKENSAVER_DIR


def _nearest_ancestor(start: Path, predicate) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if predicate(p):
            return p
    return None


def resolve_workspace(explicit: str | None = None, cwd: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    for pred in (
        lambda p: (p / TOKENSAVER_DIR / CONFIG_NAME).exists(),
        lambda p: (p / ".git").exists(),
        lambda p: (p / "CLAUDE.md").exists() or (p / "AGENTS.md").exists(),
    ):
        found = _nearest_ancestor(start, pred)
        if found:
            return found
    return start.resolve()
