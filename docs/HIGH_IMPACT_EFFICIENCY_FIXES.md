# High-Impact Efficiency Fixes

Review snapshot: `4762b82` (`main`, July 16, 2026).

This is the implementation backlog for the sequential remediation branch. Privacy work is deliberately scheduled after the efficiency phases.

## E01 — Replace quadratic stats correlation

**Where:** `src/token_saver/stats_report.py`, `correlate_pxpipe()`.

The current implementation rebuilds and filters a model's complete pxpipe candidate list for every proxy row. Observed synthetic scaling was 0.048 seconds at 1,000 rows, 0.188 seconds at 2,000, and 0.759 seconds at 4,000—roughly four times the runtime for twice the data.

Build exact-match indexes by `(model, req_body_sha8)`, timestamp-sort model candidates, use bounded-window lookup, and track consumed rows without reconstructing lists. Preserve exact-hash priority, one-to-one matching, and ambiguous transformed-match behavior.

## E02 — Bound pure-vector fallback memory

**Where:** `src/token_saver/retrieval.py`, `search()`.

On a lexical miss, search loads every chunk's text and vectors, constructs every passing `Hit`, and sorts them all. Stream metadata and vectors, keep only `top_k` in a heap, and fetch text only for winners. Preserve ranking and deterministic ties.

True hybrid/ANN candidate generation is a separate architectural task and is not part of this branch.

## E03 — Avoid complete JSONL rescans

**Where:** `src/token_saver/stats.py`, `iter_events()`, `load_events()`, and `append_event()`.

Read newest rows backward from EOF rather than reading the entire historical log. Serialize threaded appends with a lock and one encoded write. Preserve malformed-line tolerance and existing retention; automatic deletion and rotation remain disabled.

## E04 — Stream CSV sampling

**Where:** `src/token_saver/parsers.py`, `parse_csv()`.

The parser materializes every CSV row to return only the header and first 20 rows. Retain the sample and count remaining rows incrementally so memory stays proportional to sample size.

## E05 — Stream scanning and re-embedding

**Where:** `src/token_saver/indexer.py`, `scan_files()` and `index_workspace()`.

Make scanning an iterator, stream re-embedding with `fetchmany()`, batch vector updates, and protect each file with a savepoint. Report failures without committing partial replacement state.

## E06 — Strengthen incremental fingerprints

**Where:** `src/token_saver/indexer.py`, `_index_one()` and the `files` schema.

Add nanosecond metadata and a sampled BLAKE2 fingerprint. Use it to avoid full hashing unchanged files while detecting same-size, preserved-mtime replacements. Retain full SHA-256 as the authoritative identity and migrate existing indexes in place.

## E07 — Make PDF cache identity explicit

**Where:** `src/token_saver/convert.py`, `ensure_converted()` and `convert_workspace_pdfs()`.

Use atomic sidecars containing source SHA-256, size, converter version, and output hash. Reuse only matching mirrors and prune mirrors for deleted or renamed PDFs after successful indexing.

## P01 — Enforce the workspace symlink boundary

With `follow_symlinks=false`, skip every file and directory link. When enabled, follow only targets that resolve inside the workspace and prevent directory cycles. Recheck containment before reads.

## P02 — Restrict and stream source slices

Stream requested lines rather than reading whole files. Reject ignored, unindexed, changed, or escaping files; validate positive ranges and cap output at 2,000 lines. Apply equivalent CLI and MCP errors. This phase also owns the known Windows containment regression.

## Benchmark gates

- Stats correlation: 1,000, 10,000, and 100,000 rows per side.
- Search fallback: 10,000 and 100,000 chunks with large text payloads.
- JSONL reports: retained 100,000 rows in progressively larger history files.
- CSV: 1 MB and 20 MB inputs.
- Re-embedding: 10,000 and 100,000 chunks.
- Incremental index: unchanged tree, one changed file, preserved-mtime replacement, and backend switch.

Performance checks should compare growth rates or generous ceilings rather than fragile millisecond assertions.
