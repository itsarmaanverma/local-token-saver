"""Workspace config: .tokensaver/config.json (JSON, not TOML, to stay stdlib-only on py3.10)."""
from __future__ import annotations

import json
from pathlib import Path

TOKENSAVER_DIR = ".tokensaver"
CONFIG_NAME = "config.json"
INDEX_NAME = "index.sqlite"
IGNORE_NAME = ".tokensaverignore"

DEFAULT_CONFIG = {
    "retrieval": {
        "max_context_tokens": 8000,
        "max_chunks": 12,
        "max_chunks_per_file": 4,
        "max_verbatim_tokens_per_file": 2000,
        "include_summaries": True,
    },
    "indexing": {
        "target_chunk_tokens": 400,
        "max_file_bytes": 20_000_000,
        "follow_symlinks": False,
    },
}

DEFAULT_IGNORES = [
    ".git/", "node_modules/", "dist/", "build/", "target/", ".venv/", "venv/",
    "__pycache__/", "coverage/", ".tokensaver/", ".claude/", ".idea/", ".vscode/",
    "*.egg-info/", ".mypy_cache/", ".pytest_cache/", ".ruff_cache/",
    "*.lock", "*.min.js", "*.min.css", ".env*", "*.pem", "*.key", "*.sqlite",
    "*.db", "*.pyc", "*.so", "*.dll", "*.exe", "*.zip", "*.tar", "*.gz",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.mp4", "*.mp3", "*.woff*",
    "id_rsa*", "id_ed25519*", "*.p12", "*.pfx", "credentials*", "secrets*",
]


def ts_dir(root: Path) -> Path:
    return root / TOKENSAVER_DIR


def config_path(root: Path) -> Path:
    return ts_dir(root) / CONFIG_NAME


def index_path(root: Path) -> Path:
    return ts_dir(root) / INDEX_NAME


def load_config(root: Path) -> dict:
    p = config_path(root)
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    else:
        cfg = {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    for section, values in cfg.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
        else:
            merged[section] = values
    return merged


def init_workspace(root: Path) -> Path:
    """Create .tokensaver/ + config + ignore template. Idempotent."""
    root = root.resolve()
    d = ts_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    cp = config_path(root)
    if not cp.exists():
        cp.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    ip = root / IGNORE_NAME
    if not ip.exists():
        ip.write_text(
            "# Files/dirs Token Saver must never index (gitignore-style globs)\n"
            + "\n".join(DEFAULT_IGNORES) + "\n",
            encoding="utf-8",
        )
    return root
