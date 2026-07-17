# Phased Efficiency Progress

## Workspace

- Project: `local-token-saver-phased`
- Folder: `C:\Users\armaa\Desktop\local-token-saver-phased`
- Remote: `https://github.com/itsarmaanverma/local-token-saver.git`
- Branch: `codex/efficiency-phases`
- Baseline commit: `4762b820b8e782f51599b0be8d3ebce2480b6d35`
- Coordination: sequential agent-mesh; exactly one active task
- Baseline tests: 83 passed, 6 skipped, 1 known Windows containment failure

Status markers: `[ ]` pending, `[~]` active, `[!]` blocked, `[x]` complete.

## Task Checklist

- [x] **P00 — Continuity setup**
  - Status: complete
  - Owner: codex
  - Summary: Created the durable Desktop clone, phased branch, sequential mesh board, checklist, backlog, and matching Claude/Codex continuation instructions.
  - Verification: Sequential mode confirmed; a Claude-owned check was blocked from the active Codex scope while the Codex check passed. The final `any` handoff was appended for resume testing.
  - Commits: Start checkpoint `3fae635`; completion checkpoint `78471f7`.
  - Handoff: E01 is the only next task. First claim `stats_report.py`, `test_stats.py`, the benchmark script, and progress artifacts, then capture the baseline before editing correlation.
- [x] **E01 — Sub-quadratic stats correlation**
  - Status: complete
  - Owner: codex
  - Summary: Replaced per-proxy full-group scans with timestamp-sorted exact `(model, hash)` and compressed-model indexes. Added a reusable benchmark and five regressions covering ties, single consumption, exact priority, invalid windows, and used-row ambiguity.
  - Verification: Baseline 1k/2k/4k was 0.053/0.210/0.907 seconds. Optimized 1k/10k/100k median was 0.002/0.026/0.458 seconds. Stats tests passed; full suite is 89 passed, 5 skipped, and only the known P02 Windows failure. Changed-file Ruff and compileall passed.
  - Commits: Start checkpoint `d19baf6`; implementation `22efabb`; completion checkpoint is the commit containing this checklist update.
  - Handoff: Stop for user approval. When approved, E02 must first capture search-fallback ranking and peak-memory baselines before editing `retrieval.py`.
- [x] **E02 — Bounded vector-fallback memory**
  - Status: complete
  - Owner: claude
  - Summary: Replaced the full-table materialize-text-and-vectors-then-sort pure-vector fallback in `retrieval.py::search()` with a streaming `chunks LEFT JOIN vectors` scan scored into a bounded top_k min-heap keyed on `(score, -chunk_id)` for deterministic ascending-id tie-breaks, then a final `chunks` fetch for only the winning ids' text.
  - Verification: Old code at 1,000 chunks: 7.413 MB peak / 0.148s median; it crashed at 10,000/100,000 chunks with `sqlite3.OperationalError: too many SQL variables` (the old `vectors WHERE chunk_id IN (...)` built one placeholder per row -- a real correctness bug beyond the memory-efficiency target). New code succeeds at all three sizes with flat ~0.10 MB peak and 0.072/1.164/9.276s median; the 1,000-chunk ranked output is byte-identical to the old code's, confirming preserved ranking and tie-break order. Full suite is 90 passed, 5 skipped, only the known P02 Windows failure (added one regression test covering gate filtering, tie-break order, and correct text-for-winner fetch). Changed-file Ruff and compileall passed (two pre-existing, out-of-scope findings left untouched: E741 in `retrieval.py`, F401 in `test_token_saver.py`, both present on `HEAD` before this change).
  - Commits: Start checkpoint `ae0b063`; implementation `ec35044`; completion checkpoint is the commit containing this checklist update.
  - Handoff: Stop for user approval. When approved, E03 must first capture JSONL-report baselines before editing `stats.py`.
- [x] **E03 — Scalable JSONL reporting**
  - Status: complete
  - Owner: claude
  - Summary: `load_events` no longer forward-scans the whole append-only log through `iter_events` + a bounded deque. It now reads backward from EOF via a new `_iter_events_reverse` generator (fixed 64KB chunks, carried partial-line fragments across chunk boundaries, same malformed-line/missing-file tolerance as `iter_events`), stopping once `limit` valid rows are collected, then reverses to restore chronological order -- identical external contract for both callers (`proxy.py`, `stats_report.py`). `iter_events` itself is untouched. `append_event` now serializes writes through a module-level `threading.Lock` (JSON-encoded before acquiring it, mkdir+open+write as one critical section) since `proxy.py`'s `ThreadingHTTPServer` can call it concurrently from multiple request threads -- a real corruption window, not a style nit.
  - Verification: `jsonl` benchmark at 1k/10k/100k total rows (limit=100): peak memory flat at 0.186/0.241/0.239 MB and median time 0.009/0.015/0.006s -- no growth with log size, confirming the backward-read bound. 8 new regression tests, including newest-N-in-chronological-order equivalence to the old deque semantics, malformed lines near EOF, missing/empty/undersized files, and an 8-thread x 50-append concurrency test asserting all 400 resulting lines are valid uncorrupted JSON.
  - Commits: Start checkpoint `1c7897f` (shared with E04); implementation `79e1daa`; completion checkpoint is the commit containing this checklist update.
  - Handoff: Stop for user approval. When approved, E05 must first capture streaming-scan/re-embedding baselines before editing `indexer.py`.
- [x] **E04 — Streaming CSV sampling**
  - Status: complete
  - Owner: claude
  - Summary: `parse_csv` no longer builds `list(csv.reader(...))` over the whole table just to compute a 20-row sample and a count. It now streams the reader row by row, keeping only the first 20 as the sample and incrementing a counter for the rest. Preserves old behavior exactly: a `csv.Error` anywhere during iteration (header or any data row, even deep into a large file) still falls back to `parse_text()` on the whole original text rather than leaking a partial result, matching the old code's atomic all-or-nothing failure mode; a genuinely empty CSV still returns `[]`.
  - Verification: direct old-vs-new peak-memory comparison on identical input: ~4x reduction (3.854 MB -> 0.894 MB at 10k rows; 39.298 MB -> 9.477 MB at 100k rows). The residual growth with input size is the unavoidable cost of `io.StringIO(text)` copying the already-fully-read `text` parameter -- outside `parse_csv`'s contract (`parse_file` reads the file before calling it) and outside this task's scope; what this removes is the *additional* full-row-list materialization on top of that baseline. 8 new tests in new file `tests/test_parsers.py`, including a byte-identical-output check against an inline reference of the old implementation, exactly-20/25-row boundary and count correctness, header-only/empty-string edge cases, TSV delimiter handling, a mid-file `csv.Error` fallback proof, and a 5,000-row functional scale check.
  - Commits: Start checkpoint `1c7897f` (shared with E03); implementation `e515a2b`; completion checkpoint is the commit containing this checklist update.
  - Handoff: Stop for user approval. When approved, E05 must first capture streaming-scan/re-embedding baselines before editing `indexer.py`.
- [x] **E05 — Streaming scan and re-embedding**
  - Status: complete
  - Owner: claude
  - Summary: `scan_files()` is now a generator instead of a materialized `list[Path]`. The backend-mismatch re-embed pass was extracted into `_reembed_all()` and now reads chunks via `cursor.fetchmany(500)` and writes vectors via batched `executemany()` instead of `fetchall()`-ing every chunk's text and issuing one `INSERT` per chunk. Each file's replacement in `index_workspace()`'s loop is now wrapped in a new `_savepoint()` context manager (`SAVEPOINT`/`RELEASE`/`ROLLBACK TO`) -- previously a mid-file failure (e.g. an embedder error on chunk N of M) left that file's old chunks deleted and only some new chunks inserted with no rollback, silently committing corrupted partial state at the final `con.commit()`; that failure now rolls back cleanly and is counted in a new `files_failed` stat instead.
  - Verification: `reembed` benchmark at 1k/10k/100k chunks: peak memory flat at 1.74/1.639/1.656 MB (vs. growing with chunk count under the old `fetchall()`+per-row-`INSERT` approach); time scales linearly with chunk count as expected (0.85/10.5/104.9s), since embedding itself is inherently O(chunks) work -- the fix targets memory boundedness and round-trip count, not embedding speed. 4 new tests in new file `tests/test_indexer.py`: `scan_files` confirmed to be a real generator, a savepoint-rollback test that reproduces the exact corruption scenario being fixed (embed fails on a file's 2nd of 3 chunks -> zero files/chunks rows survive for that file), an `index_workspace()`-level failure-isolation test (one poisoned file fails and is reported via `files_failed` without blocking other files from indexing), and a fetchmany/executemany correctness test proving every chunk is actually re-embedded, not just a subset. Full suite: 110 passed, 5 skipped, only the known P02 Windows failure. Changed-file Ruff and compileall passed clean.
  - Commits: Start checkpoint `56ad6d2`; implementation `6abd90e`; benchmark `30ae0d3`; completion checkpoint is the commit containing this checklist update.
  - Handoff: Stop for user approval. When approved, E06 must first capture incremental-index baselines (unchanged tree, one changed file, preserved-mtime replacement, backend switch) before editing `_index_one()` and the `files` schema.
- [x] **E06 — Strong incremental fingerprints**
  - Status: complete
  - Owner: claude
  - Summary: Fixed a real correctness bug -- the old fast-path (`mtime==stored and size==stored -> skip`) cannot distinguish a genuinely unchanged file from a same-size replacement whose mtime happens to be restored to its prior value (some backup/restore/checkout tools do this); reproduced and confirmed the bug before changing anything (replace file content with same-length text, restore exact prior mtime, re-index -> stale content silently stayed indexed). `_index_one()` now does a three-tier check: `mtime_ns` (integer nanoseconds via `st_mtime_ns`, avoiding `st_mtime` float-precision pitfalls) + `size` + a cheap sampled BLAKE2 fingerprint (head/mid/tail windows, bounded to ~12KB of reads regardless of file size) must all agree to skip full hashing; if `mtime_ns`+`size` match but the fingerprint doesn't, that's exactly the same-size-preserved-mtime case and it now falls through to a full SHA-256 verification, which remains the sole authoritative identity for the metadata-drift and actually-changed branches. New `files.mtime_ns`/`files.fingerprint` columns; `connect()` now also runs `_migrate_schema()` (idempotent `ALTER TABLE ADD COLUMN`) so pre-existing on-disk indexes migrate in place -- migrated rows get safe defaults (`mtime_ns=0`, `fingerprint=''`) that can never spuriously match, forcing one real verification pass per file after upgrade, then self-healing.
  - Verification: `incremental` benchmark (cold full index vs. warm unchanged-tree re-index): 1,000 files 9.313s -> 0.385s (~24x), 5,000 files 47.120s -> 1.956s (~24x), `files_indexed_warm=0` confirming the entire tree correctly took the fast path both times. 6 new tests in `tests/test_indexer.py`: the same-size/preserved-mtime bug reproduced as a permanent regression (proves the fix), an unchanged file provably never calls `_sha256` (mocked to raise if it did), the sampled fingerprint proven to genuinely ignore changes outside its sampled windows while catching changes inside them, a pre-E06 on-disk schema migrating cleanly with correct defaults, and a migrated legacy row self-healing on its first real index run then taking the fast path afterward. Full suite: 116 passed, 5 skipped, only the known P02 Windows failure. Changed-file Ruff and compileall passed clean.
  - Commits: implementation `7e48223`; benchmark `4dc8c41`; completion checkpoint is the commit containing this checklist update. (No separate start checkpoint -- E06 began immediately after E05 completed in the same session, per user direction to do "the next two phases.")
  - Handoff: Stop for user approval. When approved, E07 must first capture PDF-cache-identity baselines before editing `convert.py`.
- [ ] **E07 — Explicit PDF cache identity**
- [ ] **P01 — Workspace symlink privacy boundary**
- [ ] **P02 — Indexed-only streaming source slices**
- [ ] **V01 — Final integration checkpoint**

## Current Task

E05 and E06 are both complete and no task is active. E07 is the next
unchecked task, but it must not begin without explicit user approval.

E05 and E06 were run one after another (not concurrently) by the same
session in response to a single user go-ahead to do "the next two phases" --
unlike E03/E04, these share a file (`indexer.py`, and E06 builds directly on
E05's `_reembed_all`/`_savepoint` helpers), so they were sequenced rather
than parallelized, consistent with the one-agent-per-file rule and this
project's default sequential mode.

E03 and E04 were run concurrently by explicit user direction as a one-off
departure from the default "exactly one active task" rule -- justified
because their file scopes were fully disjoint (`stats.py`+`test_stats.py` vs
`parsers.py`+`test_parsers.py`, no shared source/test files) and both claims
were verified non-conflicting via `mesh check` before starting. The two
implementations were built by separate parallel subagents with no shared
file access; shared artifacts (`scripts/benchmark_efficiency.py` extended in
`3cae38c`, this file, `.agent-mesh/*`) were edited by a single owner
(the orchestrating claude session) only, sequenced after both implementations
landed, to avoid the concurrent-write risk this project's sequential mode
normally guards against. Full suite after both merged: 106 passed, 5
skipped, only the known P02 Windows failure. This was a one-off, not the
new default; E05/E06 correctly reverted to strict one-at-a-time.

Completed claims: `e5bf4a76`, `cc4ef50f`, `5bc6204f`, `257cb808`, `261c8eab`

## Resume Instructions

1. Open this folder and run `git status --short --branch`.
2. Run `mesh resume --platform <claude|codex>` and read the newest handoff to that platform or `any`.
3. Read this document and work only on the first unchecked task; never start a second task concurrently.
4. Claim the task and run `mesh check` for every planned file before editing.
5. On completion, mark the item `[x]`, add a brief summary, verification results, commit SHA, and a two-line handoff.
6. Push the checkpoint, append the mesh handoff, and stop before the next task.

## Cutoff Recovery

If usage is close to its limit, do not start a new task. Leave the current working tree intact, record the dirty files and next command here, append a handoff to `any`, and stop. The next agent must inspect `git diff` before changing anything.
