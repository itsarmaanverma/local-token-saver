# ADR-0002: SQLite + FTS5 as the single index store

## Status
Accepted

## Context
The index must hold file metadata, chunk text, lexical search structures, and
vectors — per workspace, on the user's disk, with zero administration. Queries
mix exact-term lookups (identifiers, error strings) with semantic similarity.

## Decision
One SQLite database per workspace (`.tokensaver/index.sqlite`) holding
`files`, `chunks`, an FTS5 virtual table (BM25), and 384-dim float32 vector
blobs. Incremental indexing keyed on sha256 + mtime. Brute-force cosine scan
for the vector side at current scale.

## Consequences

### Positive
- Single-file, copyable, deletable index; corruption recovery = reindex.
- FTS5 BM25 is excellent for code/doc exact-term retrieval and costs nothing.
- Transactions give crash-safe incremental updates for free.

### Negative
- Brute-force cosine is O(n) per query — fine to ~10⁵ chunks, not beyond.
- Single-writer model; concurrent index runs must be serialized.

### Neutral
- Vectors as blobs keep the schema backend-agnostic (enables ADR-0003 swap
  and a future `sqlite-vec` ANN upgrade without consumer changes).

## Alternatives Considered
- **Chroma/LanceDB/FAISS** — rejected: heavy deps (violates ADR-0001), and
  none carry BM25 + metadata + vectors in one zero-ops file as cleanly.
- **Two stores (Whoosh + vector file)** — rejected: consistency between
  stores becomes the application's problem.
- **sqlite-vec now** — deferred: unnecessary below ~10⁵ chunks; planned
  upgrade path documented in ARCHITECTURE.md.
