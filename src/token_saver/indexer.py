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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import index_path, load_config, ts_dir
from .convert import ensure_converted
from .ignore import load_matcher
from .parsers import Chunk, file_type, parse_file, parse_markdown
from .vectors import Embedder, get_embedder, to_blob

_PAGE_IN_HEADING = re.compile(r"^Page (\d+)$")
_REEMBED_BATCH_SIZE = 500
_FINGERPRINT_SAMPLE_BYTES = 4096

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    sha256 TEXT NOT NULL,
    mtime REAL NOT NULL,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    ftype TEXT NOT NULL,
    ntokens INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT ''
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


def _migrate_schema(con: sqlite3.Connection) -> None:
    """Add columns introduced after a `files` table already existed on disk.

    `CREATE TABLE IF NOT EXISTS` in SCHEMA only covers brand-new indexes;
    pre-existing indexes need an explicit, idempotent ADD COLUMN so older
    on-disk databases migrate in place instead of breaking. Defaults
    (mtime_ns=0, fingerprint='') guarantee migrated rows never spuriously
    match the fast-path short-circuit -- the first re-index after upgrading
    always does one real verification pass per file, then self-heals.
    """
    existing = {row[1] for row in con.execute("PRAGMA table_info(files)").fetchall()}
    if "mtime_ns" not in existing:
        con.execute("ALTER TABLE files ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0")
    if "fingerprint" not in existing:
        con.execute("ALTER TABLE files ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")


def connect(root: Path) -> sqlite3.Connection:
    ts_dir(root).mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(index_path(root))
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate_schema(con)
    return con


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _sample_fingerprint(path: Path, size: int) -> str:
    """Cheap BLAKE2 fingerprint over the head, middle, and tail of a file.

    A fast, non-authoritative signal used only to decide whether a file is
    worth re-verifying with a full SHA-256 -- never stored or trusted as the
    file's identity. Reads at most 3x _FINGERPRINT_SAMPLE_BYTES regardless of
    file size, so it stays cheap even for very large unchanged files.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(size.to_bytes(8, "big"))
    with path.open("rb") as f:
        h.update(f.read(_FINGERPRINT_SAMPLE_BYTES))
        if size > _FINGERPRINT_SAMPLE_BYTES:
            mid = max(0, size // 2 - _FINGERPRINT_SAMPLE_BYTES // 2)
            f.seek(mid)
            h.update(f.read(_FINGERPRINT_SAMPLE_BYTES))
        if size > _FINGERPRINT_SAMPLE_BYTES * 2:
            f.seek(max(0, size - _FINGERPRINT_SAMPLE_BYTES))
            h.update(f.read(_FINGERPRINT_SAMPLE_BYTES))
    return h.hexdigest()


def scan_files(root: Path, max_bytes: int, follow_symlinks: bool) -> Iterator[Path]:
    """Walk the workspace, yielding matched file paths one at a time.

    An iterator rather than a materialized list, so a caller can start
    indexing before the walk finishes and never holds every path in memory
    at once (matters for very large workspaces).
    """
    matcher = load_matcher(root)
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
            yield p


@contextmanager
def _savepoint(con: sqlite3.Connection, name: str) -> Iterator[None]:
    """Wrap a block in a SQLite SAVEPOINT; roll it back cleanly on any error.

    Protects one file's replacement (delete-old + insert-new chunks/fts/
    vectors) as its own atomic unit within the outer per-run transaction, so
    a failure partway through a single file can never leave that file's rows
    half-written in the eventually-committed index. The original exception
    is always re-raised after rollback -- this only guarantees cleanup, it
    does not decide which failures are tolerable (the caller's except clause
    still does that).
    """
    con.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")
        raise
    else:
        con.execute(f"RELEASE {name}")


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
    """Index a single file if new/changed. Returns True if (re)indexed.

    Three-tier incremental check, cheapest first:
      1. mtime_ns + size + sampled fingerprint all match the stored row ->
         unchanged, skip full hashing entirely. mtime_ns (integer
         nanoseconds, from st_mtime_ns) avoids the float-precision pitfalls
         of comparing st_mtime directly. The sampled fingerprint additionally
         catches a same-size, preserved-mtime replacement -- a file whose
         content changed but whose mtime+size still happen to match the
         stored row (e.g. certain backup/restore or checkout tools) -- which
         mtime+size alone cannot distinguish from a genuinely unchanged file.
      2. mtime_ns/size/fingerprint don't all match -> compute the full
         SHA-256 (still the authoritative identity) to see whether content
         actually changed or just metadata drifted.
      3. Metadata drifted but SHA-256 is identical -> update stored metadata
         (including the now-current fingerprint) without re-chunking.
    """
    rel = path.relative_to(root).as_posix()
    st = path.stat()
    mtime_ns = st.st_mtime_ns
    row = con.execute(
        "SELECT id, sha256, mtime_ns, size, fingerprint FROM files WHERE path=?",
        (rel,)).fetchone()
    fingerprint = None
    if row and row[2] == mtime_ns and row[3] == st.st_size:
        fingerprint = _sample_fingerprint(path, st.st_size)
        if fingerprint == row[4]:
            return False  # unchanged by mtime+size+sampled fingerprint
    sha = _sha256(path)
    if fingerprint is None:
        fingerprint = _sample_fingerprint(path, st.st_size)
    if row and row[1] == sha:  # content identical, metadata (and/or fingerprint) drifted
        con.execute(
            "UPDATE files SET mtime=?, mtime_ns=?, size=?, fingerprint=? WHERE id=?",
            (st.st_mtime, mtime_ns, st.st_size, fingerprint, row[0]))
        return False
    if row:
        _delete_files(con, [row[0]])
    ftype = file_type(path)
    chunks = _chunks_for(root, path, rel, ftype, chunk_chars)
    total = sum(c.ntokens for c in chunks)
    cur = con.execute(
        "INSERT INTO files(path, sha256, mtime, mtime_ns, size, ftype, ntokens, fingerprint) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (rel, sha, st.st_mtime, mtime_ns, st.st_size, ftype, total, fingerprint),
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


def _reembed_all(con: sqlite3.Connection, embedder: Embedder) -> int:
    """Re-embed every existing chunk with `embedder`, streamed and batched.

    Used for the backend-mismatch pass: no re-scan, re-convert, re-chunk, or
    touching files/chunks/FTS tables. Reads chunks via fetchmany() and writes
    vectors via batched executemany() so neither side holds every chunk's
    text/vector in memory at once or round-trips one statement per chunk.
    Returns the number of chunks re-embedded.
    """
    reembedded = 0
    cursor = con.execute("SELECT id, section, text FROM chunks")
    while True:
        batch = cursor.fetchmany(_REEMBED_BATCH_SIZE)
        if not batch:
            break
        updates = [
            (cid, to_blob(embedder.embed(f"{section} {text}")))
            for cid, section, text in batch
        ]
        con.executemany(
            "INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?,?)", updates)
        reembedded += len(updates)
    return reembedded


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
    # active embedder, do a re-embed-only pass over existing chunks.
    reembedded = 0
    row = con.execute("SELECT value FROM meta WHERE key='embedding_backend'").fetchone()
    if row and row[0] and row[0] != embedder.name:
        reembedded = _reembed_all(con, embedder)

    paths = scan_files(root, int(icfg["max_file_bytes"]), bool(icfg["follow_symlinks"]))
    seen: set[str] = set()
    scanned = 0
    indexed = 0
    failed = 0
    for p in paths:
        scanned += 1
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        try:
            with _savepoint(con, "idx_file"):
                if _index_one(con, root, p, chunk_chars, embedder):
                    indexed += 1
        except (OSError, sqlite3.Error):
            failed += 1
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
    stats.update({"files_scanned": scanned, "files_indexed": indexed,
                  "files_removed": len(stale_ids),
                  "files_failed": failed,
                  "reembedded": reembedded,
                  "seconds": round(time.time() - start, 2)})
    con.close()
    return stats


def index_stats(con: sqlite3.Connection) -> dict:
    files, ftokens = con.execute("SELECT COUNT(*), COALESCE(SUM(ntokens),0) FROM files").fetchone()
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vecs = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    return {"files": files, "chunks": chunks, "indexed_tokens": ftokens, "vectors": vecs}
