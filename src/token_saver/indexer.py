"""Incremental SQLite + FTS5 index over a workspace folder."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from .config import index_path, load_config, ts_dir
from .ignore import load_matcher
from .parsers import est_tokens, file_type, parse_file

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
        rel_dir = Path(dirpath).relative_to(root)
        parts = rel_dir.parts
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") or d in (".github",)
        ]
        dirnames[:] = [d for d in dirnames if not matcher.matches_dir(parts + (d,))]
        for fname in filenames:
            p = Path(dirpath) / fname
            rel = (rel_dir / fname).as_posix()
            if rel.startswith("./"):
                rel = rel[2:]
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


def _index_one(con: sqlite3.Connection, root: Path, path: Path, chunk_chars: int) -> bool:
    """Index a single file if new/changed. Returns True if (re)indexed."""
    rel = str(path.relative_to(root).as_posix())
    st = path.stat()
    row = con.execute("SELECT id, sha256 FROM files WHERE path=?", (rel,)).fetchone()
    sha = _sha256(path)
    if row and row[1] == sha:
        return False
    if row:
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (row[0],))
        con.execute("DELETE FROM chunks WHERE file_id=?", (row[0],))
        con.execute("DELETE FROM files WHERE id=?", (row[0],))
    chunks = parse_file(path, rel, chunk_chars)
    total = sum(c.ntokens for c in chunks)
    cur = con.execute(
        "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) VALUES (?,?,?,?,?,?)",
        (rel, sha, st.st_mtime, st.st_size, file_type(path), total),
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
    return True


def index_workspace(root: Path, force: bool = False) -> dict:
    """Scan + (re)index changed files. Returns stats dict."""
    root = root.resolve()
    cfg = load_config(root)
    icfg = cfg["indexing"]
    chunk_chars = int(icfg["target_chunk_tokens"]) * 4
    con = connect(root)
    if force:
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks)")
        con.execute("DELETE FROM chunks")
        con.execute("DELETE FROM files")
    start = time.time()
    paths = scan_files(root, int(icfg["max_file_bytes"]), bool(icfg["follow_symlinks"]))
    seen = set()
    indexed = 0
    for p in paths:
        rel = str(p.relative_to(root).as_posix())
        seen.add(rel)
        try:
            if _index_one(con, root, p, chunk_chars):
                indexed += 1
        except (OSError, sqlite3.Error):
            continue
    # remove deleted files
    stale = [r[0] for r in con.execute("SELECT path FROM files").fetchall() if r[0] not in seen]
    for rel in stale:
        fid = con.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()[0]
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (fid,))
        con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
        con.execute("DELETE FROM files WHERE id=?", (fid,))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_index', ?)", (str(time.time()),))
    con.commit()
    stats = index_stats(con)
    stats.update({"files_scanned": len(paths), "files_indexed": indexed,
                  "files_removed": len(stale), "seconds": round(time.time() - start, 2)})
    con.close()
    return stats


def index_stats(con: sqlite3.Connection) -> dict:
    files, ftokens = con.execute("SELECT COUNT(*), COALESCE(SUM(ntokens),0) FROM files").fetchone()
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {"files": files, "chunks": chunks, "indexed_tokens": ftokens}
