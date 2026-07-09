"""Extractive (no-LLM) summaries of files and folders from the index.

Phase 2 will add LLM contextualization/summarization; this keeps the MVP
zero-dependency and zero-cost while still giving agents a cheap overview.
"""
from __future__ import annotations

from pathlib import Path

from .config import index_path
from .indexer import connect
from .retrieval import retrieve_context, search


def summarize_file(root: Path, rel_path: str, focus: str | None = None,
                   max_tokens: int = 1500) -> str:
    con = connect(root)
    rows = con.execute(
        "SELECT section, heading_path, start_line, end_line, page, text, ntokens "
        "FROM chunks WHERE path=? ORDER BY id", (rel_path,),
    ).fetchall()
    frow = con.execute("SELECT ftype, ntokens, size FROM files WHERE path=?",
                       (rel_path,)).fetchone()
    con.close()
    if not rows:
        return f"{rel_path}: not in index (run token-saver index)."

    out = [f"# {rel_path}",
           f"type={frow[0]} indexed_tokens={frow[1]} bytes={frow[2]} chunks={len(rows)}"]
    headings = list(dict.fromkeys(
        (r[1] or r[0]) for r in rows if (r[1] or r[0])
    ))
    if headings:
        out.append("\nStructure:")
        out.extend(f"- {h}" for h in headings[:40])
    budget = max_tokens * 4  # chars
    if focus:
        out.append(f"\nMost relevant content for {focus!r}:")
        hits = [h for h in search(root, focus, top_k=8) if h.path == rel_path]
        for h in hits:
            snippet = h.text[:budget // max(1, len(hits))]
            out.append(f"\n[{h.section or f'lines {h.start_line}-{h.end_line}'}]\n{snippet}")
    else:
        out.append("\nOpening content:")
        out.append(rows[0][5][: budget // 2])
    return "\n".join(out)


def summarize_folder(root: Path, sub: str | None = None, focus: str | None = None,
                     max_tokens: int = 2000) -> str:
    con = connect(root)
    like = f"{sub.rstrip('/')}/%" if sub else "%"
    rows = con.execute(
        "SELECT path, ftype, ntokens FROM files WHERE path LIKE ? ORDER BY ntokens DESC",
        (like,),
    ).fetchall()
    con.close()
    if not rows:
        return f"No indexed files under {sub or root}."
    total = sum(r[2] for r in rows)
    by_type: dict[str, int] = {}
    for _, ftype, _ in rows:
        by_type[ftype] = by_type.get(ftype, 0) + 1
    out = [f"# Folder summary: {root / (sub or '')}",
           f"{len(rows)} files, ~{total:,} indexed tokens",
           "By type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
           "\nLargest files (tokens):"]
    out.extend(f"- {p} ({t:,})" for p, _, t in rows[:20])
    if focus:
        out.append("\n" + retrieve_context(root, focus, max_tokens=max_tokens))
    return "\n".join(out)


def advise(root: Path, avg_payload: int = 8500) -> str:
    """Cached-full-injection vs retrieval advice (10x-payload rule from the paper)."""
    if not index_path(root).exists():
        return f"No index at {root}. Run token-saver index first."
    con = connect(root)
    total = con.execute("SELECT COALESCE(SUM(ntokens),0) FROM files").fetchone()[0]
    con.close()
    threshold = avg_payload * 10
    rec = "retrieve" if total >= threshold else "cached full injection may be cheaper"
    ratio = total / max(1, avg_payload)
    return (f"Workspace: {root}\nIndexed tokens: {total:,}\n"
            f"Average retrieval payload: {avg_payload:,}\n"
            f"10x retrieval threshold: {threshold:,}\n\n"
            f"Recommendation: {rec}\n"
            f"Reason: workspace is {ratio:.1f}x the retrieval payload "
            f"({'above' if total >= threshold else 'below'} the 10x threshold).\n"
            "Note: caching lowers dollar cost, not attention footprint — long-context "
            "degradation still applies to injected tokens.")
