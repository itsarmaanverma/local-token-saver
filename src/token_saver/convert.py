"""Automatic PDF → Markdown conversion — pure script, no LLM.

Converted Markdown mirrors live under .tokensaver/converted/<rel>.md so the
user's folder is never polluted. Conversion is cached by explicit content identity;
`token-saver index` runs it automatically before indexing, and the indexer
parses the Markdown (page-aware headings) instead of the raw PDF.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .config import ts_dir

PAGE_HEADING = re.compile(r"^## Page (\d+)$")
CONVERTER_VERSION = 1


def converted_dir(root: Path) -> Path:
    return ts_dir(root) / "converted"


def converted_path(root: Path, rel: str) -> Path:
    return converted_dir(root) / (rel + ".md")


def converted_metadata_path(root: Path, rel: str) -> Path:
    return Path(str(converted_path(root, rel)) + ".meta.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Durably write *data* beside *path*, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            tmp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def _load_metadata(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _cache_matches(out: Path, meta_path: Path, source_sha256: str,
                   source_size: int) -> bool:
    if not out.is_file() or not meta_path.is_file():
        return False
    metadata = _load_metadata(meta_path)
    if metadata != {
        "source_sha256": source_sha256,
        "source_size": source_size,
        "converter_version": CONVERTER_VERSION,
        "output_sha256": metadata.get("output_sha256") if metadata else None,
    }:
        return False
    output_sha256 = metadata.get("output_sha256")
    if not isinstance(output_sha256, str) or len(output_sha256) != 64:
        return False
    try:
        return _sha256(out) == output_sha256
    except OSError:
        return False


def pdf_to_markdown(pdf_path: Path, title: str) -> str:
    """Extract text page by page into Markdown with `## Page N` headings."""
    from pypdf import PdfReader  # hard dependency, installed with the package

    reader = PdfReader(str(pdf_path))
    parts = [f"# {title}", ""]
    for pageno, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — one bad page must not kill the doc
            text = ""
        if not text:
            continue
        parts.append(f"## Page {pageno}")
        parts.append("")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


def ensure_converted(root: Path, pdf_path: Path, rel: str, *,
                     source_sha256: str | None = None,
                     source_size: int | None = None) -> Path | None:
    """Convert rel PDF to its cached Markdown mirror if its identity is stale.

    Returns the Markdown path, or None if conversion failed.
    """
    out = converted_path(root, rel)
    meta_path = converted_metadata_path(root, rel)
    try:
        if source_size is None:
            source_size = pdf_path.stat().st_size
        if source_sha256 is None:
            source_sha256 = _sha256(pdf_path)
        if _cache_matches(out, meta_path, source_sha256, source_size):
            return out

        md = pdf_to_markdown(pdf_path, title=Path(rel).stem)
        output = md.encode("utf-8")
        metadata = {
            "source_sha256": source_sha256,
            "source_size": source_size,
            "converter_version": CONVERTER_VERSION,
            "output_sha256": hashlib.sha256(output).hexdigest(),
        }
        # The sidecar is the validity marker, so publish it only after the
        # mirror. A crash between replacements leaves an intentional miss.
        _atomic_write(out, output)
        _atomic_write(meta_path, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
        return out
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — malformed PDFs must not kill indexing
        return None


def prune_converted(root: Path, pdf_rels: list[str]) -> int:
    """Remove mirror/metadata artifacts not belonging to a live PDF."""
    cdir = converted_dir(root)
    if not cdir.exists():
        return 0
    live = {converted_path(root, rel) for rel in pdf_rels}
    pruned = 0
    for md in cdir.rglob("*.pdf.md"):
        if md not in live:
            md.unlink(missing_ok=True)
            Path(str(md) + ".meta.json").unlink(missing_ok=True)
            pruned += 1
    for metadata in cdir.rglob("*.pdf.md.meta.json"):
        mirror = Path(str(metadata)[:-len(".meta.json")])
        if mirror not in live:
            metadata.unlink(missing_ok=True)
    return pruned


def convert_workspace_pdfs(root: Path, pdf_rels: list[str]) -> dict:
    """Batch-convert PDFs; prune mirrors whose source PDF is gone."""
    ok = failed = 0
    for rel in pdf_rels:
        if ensure_converted(root, root / rel, rel) is not None:
            ok += 1
        else:
            failed += 1
    pruned = prune_converted(root, pdf_rels)
    return {"pdfs_converted": ok, "pdfs_failed": failed, "pdfs_pruned": pruned}
