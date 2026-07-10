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
from pathlib import Path

REQUIRED_PACKAGES = [("pypdf", "pypdf>=4")]

EMBEDDING_PACKAGES = [("onnxruntime", "onnxruntime>=1.16"), ("tokenizers", "tokenizers>=0.15")]


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


def _download_verified(url: str, dest: Path, expected_sha256: str) -> tuple[bool, str]:
    import urllib.request
    import hashlib
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)
    except OSError as e:
        return False, f"[FAIL] download {dest.name}: {e}"
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if digest != expected_sha256:
        tmp.unlink(missing_ok=True)
        return False, f"[FAIL] {dest.name} sha256 mismatch (got {digest[:12]}..., expected {expected_sha256[:12]}...)"
    tmp.replace(dest)
    return True, f"[ok] downloaded + verified {dest.name}"


def setup_embeddings(auto_install: bool = True, download_model: bool = True) -> tuple[bool, str]:
    """Install onnxruntime+tokenizers and download the pinned ONNX MiniLM model.

    Opt-in path for `token-saver setup --with-embeddings`. Never runs unless
    explicitly requested by the caller.
    """
    from .embeddings_onnx import (
        HF_REPO, HF_REVISION, MODEL_SHA256, TOKENIZER_SHA256,
        MODEL_FILE, TOKENIZER_FILE, cache_dir,
    )
    lines = []
    ok = True
    for module, spec in EMBEDDING_PACKAGES:
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

    if not download_model:
        lines.append("[skip] model download skipped (--no-embeddings-model)")
        return ok, "\n".join(lines)

    d = cache_dir()
    model_path = d / MODEL_FILE
    tok_path = d / TOKENIZER_FILE
    base_url = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}"

    for path, url, expected_sha in (
        (model_path, f"{base_url}/onnx/{MODEL_FILE}", MODEL_SHA256),
        (tok_path, f"{base_url}/{TOKENIZER_FILE}", TOKENIZER_SHA256),
    ):
        if path.exists():
            import hashlib
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() == expected_sha:
                lines.append(f"[ok] {path.name} already present + verified")
                continue
            lines.append(f"[warn] {path.name} present but hash mismatch — re-downloading")
        dl_ok, msg = _download_verified(url, path, expected_sha)
        lines.append(msg)
        ok = ok and dl_ok

    lines.append("Embedding model ready" if ok else "Embedding model setup incomplete — fix the failures above.")
    return ok, "\n".join(lines)
