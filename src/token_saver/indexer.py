"""Incremental SQLite + FTS5 + vector index over a workspace folder.

Pipeline per index run (all script-based, no LLM):
  scan → PDF-to-Markdown conversion (cached) → parse/chunk → FTS5 + vectors (pluggable backend)
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import index_path, load_config, ts_dir
from .convert import ensure_converted, prune_converted
from .ignore import load_matcher
from .parsers import Chunk, file_type, parse_file, parse_markdown
from .vectors import Embedder, get_embedder, to_blob

_PAGE_IN_HEADING = re.compile(r"^Page (\d+)$")
_REEMBED_BATCH_SIZE = 500
_FINGERPRINT_SAMPLE_BYTES = 4096


class UnsafeWorkspacePath(OSError):
    """A path cannot be read without crossing the configured workspace boundary."""

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


def _is_link_or_reparse(path: Path) -> bool:
    """Recognize POSIX links plus Windows junction/reparse-point entries."""
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag)


def _is_within(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(root)),
                                   os.path.normcase(str(target)))) == os.path.normcase(str(root))
    except ValueError:  # different Windows drives
        return False


def _authorized_read_path(root: Path, path: Path, follow_symlinks: bool) -> Path:
    """Resolve a logical workspace path under the active link policy."""
    resolved_root = root.resolve(strict=True)
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise UnsafeWorkspacePath(f"path escapes workspace: {path}") from exc

    if not follow_symlinks:
        relative = path.absolute().relative_to(root.absolute())
        current = root.absolute()
        for part in relative.parts:
            current /= part
            if _is_link_or_reparse(current):
                raise UnsafeWorkspacePath(f"linked path disabled: {path}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafeWorkspacePath(f"unresolvable workspace path: {path}") from exc
    if not _is_within(resolved_root, resolved):
        raise UnsafeWorkspacePath(f"path target escapes workspace: {path}")
    return resolved


def _has_hidden_directory(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part.startswith(".") and part != ".github" for part in parts[:-1])


def scan_files(root: Path, max_bytes: int, follow_symlinks: bool, *,
               unsafe_files: set[str] | None = None,
               unsafe_dirs: set[str] | None = None) -> Iterator[Path]:
    """Walk the workspace, yielding matched file paths one at a time.

    An iterator rather than a materialized list, so a caller can start
    indexing before the walk finishes and never holds every path in memory
    at once (matters for very large workspaces).
    """
    root = root.absolute()
    resolved_root = root.resolve(strict=True)
    matcher = load_matcher(root)
    seen_dirs = {os.path.normcase(str(resolved_root))}
    seen_files: set[str] = set()
    unsafe_files = unsafe_files if unsafe_files is not None else set()
    unsafe_dirs = unsafe_dirs if unsafe_dirs is not None else set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        allowed_dirs = []
        for dirname in sorted(dirnames):
            logical = Path(dirpath) / dirname
            rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
            if (dirname.startswith(".") and dirname != ".github") or matcher.matches_dir(rel):
                continue
            try:
                target = _authorized_read_path(root, logical, follow_symlinks)
            except UnsafeWorkspacePath:
                unsafe_dirs.add(rel)
                continue
            target_rel = target.relative_to(resolved_root).as_posix()
            if _has_hidden_directory(f"{target_rel}/placeholder") or matcher.matches_dir(target_rel):
                continue
            identity = os.path.normcase(str(target))
            if identity in seen_dirs:
                continue
            seen_dirs.add(identity)
            allowed_dirs.append(dirname)
        dirnames[:] = allowed_dirs

        for fname in sorted(filenames):
            p = Path(dirpath) / fname
            rel = f"{rel_dir}/{fname}" if rel_dir else fname
            if matcher.matches_file(rel):
                continue
            try:
                target = _authorized_read_path(root, p, follow_symlinks)
            except UnsafeWorkspacePath:
                unsafe_files.add(rel)
                continue
            target_rel = target.relative_to(resolved_root).as_posix()
            if _has_hidden_directory(target_rel) or matcher.matches_file(target_rel):
                continue
            identity = os.path.normcase(str(target))
            if follow_symlinks and identity in seen_files:
                continue
            seen_files.add(identity)
            try:
                st = target.stat()
            except OSError:
                continue
            if not target.is_file() or st.st_size == 0 or st.st_size > max_bytes:
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


def _chunks_for(root: Path, path: Path, rel: str, ftype: str, chunk_chars: int,
                source_sha256: str | None = None,
                source_size: int | None = None) -> list[Chunk]:
    """Parse a file; PDFs go through the cached Markdown mirror first."""
    if ftype != "pdf":
        return parse_file(path, rel, chunk_chars)
    md = ensure_converted(root, path, rel, source_sha256=source_sha256,
                          source_size=source_size)
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
                embedder: Embedder, follow_symlinks: bool = False) -> bool:
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
    read_path = _authorized_read_path(root, path, follow_symlinks)
    st = read_path.stat()
    mtime_ns = st.st_mtime_ns
    row = con.execute(
        "SELECT id, sha256, mtime_ns, size, fingerprint FROM files WHERE path=?",
        (rel,)).fetchone()
    fingerprint = None
    if row and row[2] == mtime_ns and row[3] == st.st_size:
        read_path = _authorized_read_path(root, path, follow_symlinks)
        fingerprint = _sample_fingerprint(read_path, st.st_size)
        if fingerprint == row[4]:
            return False  # unchanged by mtime+size+sampled fingerprint
    read_path = _authorized_read_path(root, path, follow_symlinks)
    sha = _sha256(read_path)
    if fingerprint is None:
        read_path = _authorized_read_path(root, path, follow_symlinks)
        fingerprint = _sample_fingerprint(read_path, st.st_size)
    if row and row[1] == sha:  # content identical, metadata (and/or fingerprint) drifted
        con.execute(
            "UPDATE files SET mtime=?, mtime_ns=?, size=?, fingerprint=? WHERE id=?",
            (st.st_mtime, mtime_ns, st.st_size, fingerprint, row[0]))
        return False
    if row:
        _delete_files(con, [row[0]])
    read_path = _authorized_read_path(root, path, follow_symlinks)
    ftype = file_type(path)  # preserve the logical workspace path's format contract
    chunks = _chunks_for(root, read_path, rel, ftype, chunk_chars, sha, st.st_size)
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

    follow_symlinks = bool(icfg["follow_symlinks"])
    unsafe_files: set[str] = set()
    unsafe_dirs: set[str] = set()
    paths = scan_files(root, int(icfg["max_file_bytes"]), follow_symlinks,
                       unsafe_files=unsafe_files, unsafe_dirs=unsafe_dirs)
    seen: set[str] = set()
    pdf_rels: list[str] = []
    scanned = 0
    indexed = 0
    failed = 0
    for p in paths:
        scanned += 1
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        if p.suffix.lower() == ".pdf":
            pdf_rels.append(rel)
        try:
            with _savepoint(con, "idx_file"):
                if _index_one(con, root, p, chunk_chars, embedder, follow_symlinks):
                    indexed += 1
        except (OSError, sqlite3.Error):
            failed += 1
            continue
    # remove deleted files — ids captured in one query, batched delete
    def retained_unsafe(rel: str) -> bool:
        return rel in unsafe_files or any(
            rel == directory or rel.startswith(directory + "/") for directory in unsafe_dirs)

    stale_ids = [fid for fid, rel in con.execute("SELECT id, path FROM files").fetchall()
                 if rel not in seen and not retained_unsafe(rel)]
    _delete_files(con, stale_ids)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_index', ?)",
                (str(time.time()),))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('embedding_backend', ?)",
                (embedder.name,))
    con.commit()
    # The database is now durable, so filesystem cache cleanup cannot leave
    # an otherwise successful indexing transaction half-applied.
    prune_converted(root, pdf_rels)
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
