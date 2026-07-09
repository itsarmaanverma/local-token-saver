"""Dependency setup: verify/install everything the pipeline needs.

The PDF → Markdown → vectorizer flow is fully script-based (no LLM):
- pypdf: PDF text extraction (the only third-party dependency)
- SQLite FTS5: lexical index (ships with Python's sqlite3)
- hashed TF vectors: pure stdlib (vectors.py) — no model downloads

`token-saver setup` checks each and pip-installs what's missing.
"""
from __future__ import annotations

import importlib
import sqlite3
import subprocess
import sys

REQUIRED_PACKAGES = [("pypdf", "pypdf>=4")]


def _fts5_ok() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


def setup(auto_install: bool = True) -> tuple[bool, str]:
    """Verify/install pipeline dependencies. Returns (ok, report)."""
    lines = []
    ok = True
    for module, spec in REQUIRED_PACKAGES:
        try:
            importlib.import_module(module)
            lines.append(f"[ok] {module} installed")
            continue
        except ImportError:
            pass
        if not auto_install:
            lines.append(f"[missing] {module} — run: pip install '{spec}'")
            ok = False
            continue
        lines.append(f"[installing] {spec} ...")
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", spec],
            capture_output=True, text=True, timeout=600,
        )
        if res.returncode == 0:
            importlib.invalidate_caches()
            try:
                importlib.import_module(module)
                lines.append(f"[ok] {module} installed")
                continue
            except ImportError:
                pass
        lines.append(f"[FAIL] could not install {spec}: {res.stderr.strip()[:200]}")
        ok = False
    if _fts5_ok():
        lines.append("[ok] SQLite FTS5 available")
    else:
        lines.append("[FAIL] SQLite FTS5 missing — reinstall Python with FTS5-enabled sqlite3")
        ok = False
    lines.append("[ok] vectorizer: built-in hashed TF (no downloads needed)")
    lines.append("Pipeline ready: PDF -> Markdown -> chunks -> FTS5 + vectors"
                 if ok else "Pipeline NOT ready — fix the failures above.")
    return ok, "\n".join(lines)
