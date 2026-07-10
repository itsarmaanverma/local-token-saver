"""Incremental SQLite + FTS5 + vector index over a workspace folder.

Pipeline per index run (all script-based, no LLM):
  scan → PDF-to-Markdown conversion (cached) → parse/chunk → FTS5 + vectors (pluggable backend)
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import index_path, load_config, ts_dir
from .convert import ensure_converted
from .ignore import load_matcher
from .parsers import Chunk, file_type, parse_file, parse_markdown
from .vectors import Embedder, get_embedder, to_blob

_PAGE_IN_HEADING = re.compile(r"^Page (\d+)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    sha256 TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    ftype TEXT NOT NULL,
    ntokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    section TEXT DEFAULT '',
    heading_path TEXT DEFAULT '',
    start_line INTEGER,
    end_line INTEGER,
    page INTEGER,
    text TEXT NOT NULL,
    ntokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, path, section, content='chunks', content_rowid='id'
);
CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(root: Path) -> sqlite3.Connection:
    ts_dir(root).mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(index_path(root))
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def scan_files(root: Path, max_bytes: int, follow_symlinks: bool) -> list[Path]:
    matcher = load_matcher(root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d for d in dirnames
            if (not d.startswith(".") or d in (".github",))
            and not matcher.matches_dir(f"{rel_dir}/{d}" if rel_dir else d)
        ]
        for fname in filenames:
            p = Path(dirpath) / fname
            rel = f"{rel_dir}/{fname}" if rel_dir else fname
            if matcher.matches_file(rel):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size == 0 or st.st_size > max_bytes:
                continue
            out.append(p)
    return out


def _delete_files(con: sqlite3.Connection, fids: list[int]) -> None:
    """Batched removal of files + chunks + FTS rows (vectors cascade)."""
    for i in range(0, len(fids), 500):
        batch = fids[i: i + 500]
        marks = ",".join("?" * len(batch))
        con.execute(
            f"DELETE FROM chunks_fts WHERE rowid IN "
            f"(SELECT id FROM chunks WHERE file_id IN ({marks}))", batch)
        con.execute(f"DELETE FROM chunks WHERE file_id IN ({marks})", batch)
        con.execute(f"DELETE FROM files WHERE id IN ({marks})", batch)


def _chunks_for(root: Path, path: Path, rel: str, ftype: str, chunk_chars: int) -> list[Chunk]:
    """Parse a file; PDFs go through the cached Markdown mirror first."""
    if ftype != "pdf":
        return parse_file(path, rel, chunk_chars)
    md = ensure_converted(root, path, rel)
    if md is None:
        return [Chunk(rel, f"[PDF not converted: extraction failed or pypdf missing] {rel}",
                      "unindexed", [], 1, 1)]
    text = md.read_text(encoding="utf-8", errors="replace")
    chunks = parse_markdown(text, rel, chunk_chars)
    for c in chunks:  # map `## Page N` headings back to page numbers
        for h in reversed(c.heading_path):
            m = _PAGE_IN_HEADING.match(h)
            if m:
                c.page = int(m.group(1))
                break
    return chunks


def _index_one(con: sqlite3.Connection, root: Path, path: Path, chunk_chars: int,
                embedder: Embedder) -> bool:
    """Index a single file if new/changed. Returns True if (re)indexed."""
    rel = path.relative_to(root).as_posix()
    st = path.stat()
    row = con.execute("SELECT id, sha256, mtime, size FROM files WHERE path=?",
                      (rel,)).fetchone()
    if row and row[2] == st.st_mtime and row[3] == st.st_size:
        return False  # unchanged by mtime+size — skip reading/hashing entirely
    sha = _sha256(path)
    if row and row[1] == sha:  # content identical, metadata drifted
        con.execute("UPDATE files SET mtime=?, size=? WHERE id=?",
                    (st.st_mtime, st.st_size, row[0]))
        return False
    if row:
        _delete_files(con, [row[0]])
    ftype = file_type(path)
    chunks = _chunks_for(root, path, rel, ftype, chunk_chars)
    total = sum(c.ntokens for c in chunks)
    cur = con.execute(
        "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) VALUES (?,?,?,?,?,?)",
        (rel, sha, st.st_mtime, st.st_size, ftype, total),
    )
    fid = cur.lastrowid
    for c in chunks:
        ccur = con.execute(
            "INSERT INTO chunks(file_id, path, section, heading_path, start_line, end_line, page, text, ntokens) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, rel, c.section, " > ".join(c.heading_path), c.start_line,
             c.end_line, c.page, c.text, c.ntokens),
        )
        con.execute(
            "INSERT INTO chunks_fts(rowid, text, path, section) VALUES (?,?,?,?)",
            (ccur.lastrowid, c.text, rel, c.section),
        )
        con.execute(
            "INSERT INTO vectors(chunk_id, vec) VALUES (?,?)",
            (ccur.lastrowid, to_blob(embedder.embed(f"{c.section} {c.text}"))),
        )
    return True


def index_workspace(root: Path, force: bool = False) -> dict:
    """Scan + convert PDFs + (re)index changed files. Returns stats dict."""
    root = root.resolve()
    cfg = load_config(root)
    icfg = cfg["indexing"]
    chunk_chars = int(icfg["target_chunk_tokens"]) * 4
    embedder = get_embedder(cfg)  # constructed once per run — ONNX loads a model in __init__
    con = connect(root)
    if force:
        all_ids = [r[0] for r in con.execute("SELECT id FROM files").fetchall()]
        _delete_files(con, all_ids)
    start = time.time()

    # Backend-mismatch detection: if the stored backend differs from the
    # active embedder, do a re-embed-only pass over existing chunks — no
    # re-scan, re-convert, re-chunk, or touching files/chunks/FTS tables.
    reembedded = 0
    row = con.execute("SELECT value FROM meta WHERE key='embedding_backend'").fetchone()
    if row and row[0] and row[0] != embedder.name:
        for cid, section, text in con.execute("SELECT id, section, text FROM chunks").fetchall():
            vec = embedder.embed(f"{section} {text}")
            con.execute("INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?,?)",
                        (cid, to_blob(vec)))
            reembedded += 1

    paths = scan_files(root, int(icfg["max_file_bytes"]), bool(icfg["follow_symlinks"]))
    seen: set[str] = set()
    indexed = 0
    for p in paths:
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        try:
            if _index_one(con, root, p, chunk_chars, embedder):
                indexed += 1
        except (OSError, sqlite3.Error):
            continue
    # remove deleted files — ids captured in one query, batched delete
    stale_ids = [fid for fid, rel in con.execute("SELECT id, path FROM files").fetchall()
                 if rel not in seen]
    _delete_files(con, stale_ids)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_index', ?)",
                (str(time.time()),))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('embedding_backend', ?)",
                (embedder.name,))
    con.commit()
    stats = index_stats(con)
    stats.update({"files_scanned": len(paths), "files_indexed": indexed,
                  "files_removed": len(stale_ids),
                  "reembedded": reembedded,
                  "seconds": round(time.time() - start, 2)})
    con.close()
    return stats


def index_stats(con: sqlite3.Connection) -> dict:
    files, ftokens = con.execute("SELECT COUNT(*), COALESCE(SUM(ntokens),0) FROM files").fetchone()
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vecs = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    return {"files": files, "chunks": chunks, "indexed_tokens": ftokens, "vectors": vecs}
