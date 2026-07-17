"""Repeatable performance probes for phased efficiency work.

Run from an installed checkout, for example:

    python scripts/benchmark_efficiency.py stats --sizes 1000,10000,100000
    python scripts/benchmark_efficiency.py search --sizes 10000,100000
    python scripts/benchmark_efficiency.py jsonl --sizes 1000,10000,100000
    python scripts/benchmark_efficiency.py csv --sizes 1000,10000,100000
    python scripts/benchmark_efficiency.py reembed --sizes 10000,100000
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

from token_saver.stats_report import correlate_pxpipe


def _stats_rows(count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create chronological, uniquely hash-matched proxy/pxpipe rows."""
    start_ms = 1_700_000_000_000
    proxy_rows: list[dict[str, Any]] = []
    pxpipe_rows: list[dict[str, Any]] = []
    for index in range(count):
        timestamp = start_ms + index * 1000
        digest = f"{index:08x}"
        proxy_rows.append({
            "ts": timestamp,
            "model": "claude-sonnet-benchmark",
            "req_body_sha8": digest,
        })
        pxpipe_rows.append({
            "ts": timestamp,
            "model": "claude-sonnet-benchmark",
            "req_body_sha8": digest,
            "compressed": True,
        })
    return proxy_rows, pxpipe_rows


def benchmark_stats(count: int, repeat: int) -> dict[str, int | float]:
    proxy_rows, pxpipe_rows = _stats_rows(count)
    timings = []
    for _ in range(repeat):
        started = time.perf_counter()
        matched, exact, transformed, ambiguous = correlate_pxpipe(proxy_rows, pxpipe_rows)
        timings.append(time.perf_counter() - started)
        if (len(matched), exact, transformed, ambiguous) != (count, count, 0, 0):
            raise RuntimeError("correlation benchmark produced incorrect match counts")
    return {
        "rows_per_side": count,
        "median_seconds": round(statistics.median(timings), 6),
        "repeat": repeat,
    }


def _search_workspace(count: int, text_words: int = 400) -> Path:
    """Build a synthetic indexed workspace with `count` large-payload chunks.

    Roughly 1 in 40 chunks gets two query-marker tokens spliced into its
    `chunks.text` (and therefore its vector), so the vector gate has real
    winners to rank instead of exercising an always-empty fast path. The
    `chunks_fts` copy is populated from the pre-splice word list, so it
    never contains the marker tokens: FTS5 always misses the benchmark
    query, which is what forces search() into the pure-vector fallback
    this benchmark targets, rather than the bounded lexical-hit path.
    """
    from token_saver.config import init_workspace
    from token_saver.indexer import connect
    from token_saver.vectors import HashedTFEmbedder, to_blob

    root = Path(tempfile.mkdtemp(prefix="tsbench_search_"))
    init_workspace(root)
    con = connect(root)
    embedder = HashedTFEmbedder()
    vocab = [f"lexeme{i}" for i in range(500)]
    rng = random.Random(1234)
    for i in range(count):
        rel = f"doc_{i}.md"
        section = f"Section {i}"
        words = rng.choices(vocab, k=text_words)
        fts_text = " ".join(words)  # indexed as-is: never contains the query markers
        if i % 40 == 0:
            # hashed_tf is term-frequency weighted, so a single marker
            # occurrence is swamped by ~400 noise words and never clears the
            # 0.35 gate. Repeating the pair concentrates enough mass in the
            # marker's hashed dimensions to produce a real, gate-passing hit.
            words[:40] = ["benchqueryalpha", "benchquerybeta"] * 20
        text = " ".join(words)
        cur = con.execute(
            "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) VALUES (?,?,?,?,?,?)",
            (rel, f"{i:064x}", 0.0, len(text), "md", text_words),
        )
        fid = cur.lastrowid
        ccur = con.execute(
            "INSERT INTO chunks(file_id, path, section, heading_path, start_line, end_line, "
            "page, text, ntokens) VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, rel, section, section, 1, 10, None, text, text_words),
        )
        cid = ccur.lastrowid
        con.execute(
            "INSERT INTO chunks_fts(rowid, text, path, section) VALUES (?,?,?,?)",
            (cid, fts_text, rel, section),
        )
        con.execute(
            "INSERT INTO vectors(chunk_id, vec) VALUES (?,?)",
            (cid, to_blob(embedder.embed(f"{section} {text}"))),
        )
    con.commit()
    con.close()
    return root


def benchmark_search(count: int, repeat: int) -> dict[str, Any]:
    """Peak-memory and ranking probe for the pure-vector fallback in search()."""
    from token_saver.retrieval import search

    root = _search_workspace(count)
    try:
        query = "benchqueryalpha benchquerybeta"
        timings = []
        peak_bytes = 0
        top_ids: list[int] = []
        for _ in range(repeat):
            tracemalloc.start()
            started = time.perf_counter()
            hits = search(root, query, top_k=20)
            timings.append(time.perf_counter() - started)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_bytes = max(peak_bytes, peak)
            top_ids = [h.chunk_id for h in hits]
        return {
            "chunks": count,
            "median_seconds": round(statistics.median(timings), 6),
            "peak_memory_mb": round(peak_bytes / (1024 * 1024), 3),
            "hits_returned": len(top_ids),
            "top_hit_ids": top_ids,
            "repeat": repeat,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def benchmark_jsonl(count: int, repeat: int) -> dict[str, int | float]:
    """Benchmark load_events with synthetic append-only JSONL log."""
    from token_saver.stats import load_events

    root = Path(tempfile.mkdtemp(prefix="tsbench_jsonl_"))
    log_path = root / "events.jsonl"

    try:
        # Write synthetic JSONL
        with open(log_path, "w") as f:
            for i in range(count):
                row = {"ts": i, "method": "POST", "status": 200}
                f.write(json.dumps(row) + "\n")

        timings = []
        peak_bytes = 0
        rows_returned = 0
        for _ in range(repeat):
            tracemalloc.start()
            started = time.perf_counter()
            rows = load_events(str(log_path), limit=100)
            timings.append(time.perf_counter() - started)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_bytes = max(peak_bytes, peak)
            rows_returned = len(rows)
        if rows_returned != min(count, 100):
            raise RuntimeError("jsonl benchmark returned an unexpected row count")

        return {
            "total_rows": count,
            "limit": 100,
            "rows_returned": rows_returned,
            "median_seconds": round(statistics.median(timings), 6),
            "peak_memory_mb": round(peak_bytes / (1024 * 1024), 3),
            "repeat": repeat,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def benchmark_csv(count: int, repeat: int) -> dict[str, int | float]:
    """Benchmark parse_csv with synthetic large CSV."""
    from token_saver.parsers import parse_csv

    # Build synthetic CSV
    header = "id,method,status,message\n"
    rows = [f"{i},POST,200,msg_{i}\n" for i in range(count)]
    text = header + "".join(rows)

    timings = []
    peak_bytes = 0
    chunks_returned = 0
    for _ in range(repeat):
        tracemalloc.start()
        started = time.perf_counter()
        chunks = parse_csv(text, "bench.csv", 1600)
        timings.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_bytes = max(peak_bytes, peak)
        chunks_returned = len(chunks)
    if chunks_returned != 1:
        raise RuntimeError("csv benchmark produced an unexpected chunk count")

    return {
        "data_rows": count,
        "median_seconds": round(statistics.median(timings), 6),
        "peak_memory_mb": round(peak_bytes / (1024 * 1024), 3),
        "repeat": repeat,
    }


def _reembed_workspace(count: int) -> Path:
    """Build a workspace index with `count` pre-existing chunks needing
    re-embedding (embedding_backend meta deliberately mismatched)."""
    from token_saver.config import init_workspace
    from token_saver.indexer import connect
    from token_saver.vectors import HashedTFEmbedder, to_blob

    root = Path(tempfile.mkdtemp(prefix="tsbench_reembed_"))
    init_workspace(root)
    con = connect(root)
    embedder = HashedTFEmbedder()
    for i in range(count):
        rel = f"doc_{i}.md"
        text = f"Section {i} body content number {i} for reembedding benchmark."
        cur = con.execute(
            "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) VALUES (?,?,?,?,?,?)",
            (rel, f"{i:064x}", 0.0, len(text), "md", 20),
        )
        fid = cur.lastrowid
        ccur = con.execute(
            "INSERT INTO chunks(file_id, path, section, heading_path, start_line, end_line, "
            "page, text, ntokens) VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, rel, f"Section {i}", "", 1, 1, None, text, 20),
        )
        con.execute(
            "INSERT INTO vectors(chunk_id, vec) VALUES (?,?)",
            (ccur.lastrowid, to_blob(embedder.embed(text))),
        )
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('embedding_backend', 'stale_backend')")
    con.commit()
    con.close()
    return root


def benchmark_reembed(count: int, repeat: int) -> dict[str, int | float]:
    """Benchmark the streamed fetchmany()+executemany() re-embed pass."""
    from token_saver.indexer import _reembed_all, connect
    from token_saver.vectors import HashedTFEmbedder

    root = _reembed_workspace(count)
    try:
        embedder = HashedTFEmbedder()
        timings = []
        peak_bytes = 0
        reembedded = 0
        for _ in range(repeat):
            con = connect(root)
            tracemalloc.start()
            started = time.perf_counter()
            reembedded = _reembed_all(con, embedder)
            timings.append(time.perf_counter() - started)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_bytes = max(peak_bytes, peak)
            con.close()
        if reembedded != count:
            raise RuntimeError("reembed benchmark re-embedded an unexpected chunk count")
        return {
            "chunks": count,
            "reembedded": reembedded,
            "median_seconds": round(statistics.median(timings), 6),
            "peak_memory_mb": round(peak_bytes / (1024 * 1024), 3),
            "repeat": repeat,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="case", required=True)
    stats = subparsers.add_parser("stats", help="benchmark pxpipe correlation")
    stats.add_argument("--sizes", default="1000,10000,100000")
    stats.add_argument("--repeat", type=_positive_int, default=1)
    search = subparsers.add_parser("search", help="benchmark vector-fallback search()")
    search.add_argument("--sizes", default="10000,100000")
    search.add_argument("--repeat", type=_positive_int, default=1)
    jsonl = subparsers.add_parser("jsonl", help="benchmark load_events on synthetic JSONL")
    jsonl.add_argument("--sizes", default="1000,10000,100000")
    jsonl.add_argument("--repeat", type=_positive_int, default=1)
    csv = subparsers.add_parser("csv", help="benchmark parse_csv on synthetic CSV")
    csv.add_argument("--sizes", default="1000,10000,100000")
    csv.add_argument("--repeat", type=_positive_int, default=1)
    reembed = subparsers.add_parser("reembed", help="benchmark streamed backend-mismatch re-embed")
    reembed.add_argument("--sizes", default="10000,100000")
    reembed.add_argument("--repeat", type=_positive_int, default=1)
    args = parser.parse_args()

    if args.case == "stats":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "stats_correlation",
            "results": [benchmark_stats(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    if args.case == "search":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "vector_fallback_search",
            "results": [benchmark_search(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    if args.case == "jsonl":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "load_events_jsonl",
            "results": [benchmark_jsonl(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    if args.case == "csv":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "parse_csv_streaming",
            "results": [benchmark_csv(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    if args.case == "reembed":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "streamed_reembed",
            "results": [benchmark_reembed(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    raise AssertionError(f"unhandled benchmark case: {args.case}")


if __name__ == "__main__":
    raise SystemExit(main())
