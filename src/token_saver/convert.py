"""Automatic PDF → Markdown conversion — pure script, no LLM.

Converted Markdown mirrors live under .tokensaver/converted/<rel>.md so the
user's folder is never polluted. Conversion is cached by source mtime+size;
`token-saver index` runs it automatically before indexing, and the indexer
parses the Markdown (page-aware headings) instead of the raw PDF.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import ts_dir

PAGE_HEADING = re.compile(r"^## Page (\d+)$")


def converted_dir(root: Path) -> Path:
    return ts_dir(root) / "converted"


def converted_path(root: Path, rel: str) -> Path:
    return converted_dir(root) / (rel + ".md")


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


def ensure_converted(root: Path, pdf_path: Path, rel: str) -> Path | None:
    """Convert rel PDF to its cached Markdown mirror if missing/stale.

    Returns the Markdown path, or None if conversion failed.
    """
    out = converted_path(root, rel)
    try:
        src_stat = pdf_path.stat()
        if out.exists():
            o = out.stat()
            if o.st_mtime >= src_stat.st_mtime and o.st_size > 0:
                return out
        out.parent.mkdir(parents=True, exist_ok=True)
        md = pdf_to_markdown(pdf_path, title=Path(rel).stem)
        out.write_text(md, encoding="utf-8")
        return out
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — malformed PDFs must not kill indexing
        return None


def convert_workspace_pdfs(root: Path, pdf_rels: list[str]) -> dict:
    """Batch-convert PDFs; prune mirrors whose source PDF is gone."""
    ok = failed = 0
    for rel in pdf_rels:
        if ensure_converted(root, root / rel, rel) is not None:
            ok += 1
        else:
            failed += 1
    cdir = converted_dir(root)
    pruned = 0
    if cdir.exists():
        live = {str(converted_path(root, rel)) for rel in pdf_rels}
        for md in cdir.rglob("*.pdf.md"):
            if str(md) not in live:
                md.unlink(missing_ok=True)
                pruned += 1
    return {"pdfs_converted": ok, "pdfs_failed": failed, "pdfs_pruned": pruned}
