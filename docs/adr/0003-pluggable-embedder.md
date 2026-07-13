# ADR-0003: Pluggable embedder; opt-in ONNX MiniLM INT8 tier

## Status
Accepted

## Context
Real sentence embeddings materially improve semantic recall, but pulling
torch/onnxruntime into the default install violates ADR-0001, and any network
download at install time is a trust and reliability problem.

## Decision
Define one `Embedder` interface producing 384-dim L2-normalized float32
vectors, with two interchangeable backends:
- `hashed_tf` (default): hashed TF over word unigrams+bigrams — stdlib-only,
  deterministic, no download.
- `onnx_minilm` (opt-in): Xenova/all-MiniLM-L6-v2 `model_quantized.onnx`
  (INT8, ~23 MB, Apache-2.0) via `onnxruntime` + `tokenizers`, installed by
  `token-saver setup --with-embeddings` with a pinned repo/revision and
  sha256-verified atomic download. The embedder module itself never downloads;
  missing deps raise `EmbedderUnavailable` and callers fall back to hashed-TF.

## Consequences

### Positive
- Default install keeps working offline and deterministically.
- Same 384-dim shape → drop-in replacement in the `vectors` table; retrieval
  code is backend-blind.
- Supply chain: one pinned artifact, hash-checked, opt-in.

### Negative
- Two quality tiers to reason about in bug reports.
- Switching backends without reindexing mixes vector spaces (tracked risk;
  mitigation is forcing a vector rebuild on backend change).

## Alternatives Considered
- **sentence-transformers** — rejected: torch dependency (~GBs) for a 23 MB
  model's worth of value.
- **Remote embedding APIs** — rejected: breaks the local-only privacy NFR and
  adds per-index cost.
- **Different dims per backend** — rejected: would leak backend identity into
  schema and retrieval.
