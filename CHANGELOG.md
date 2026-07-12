# Changelog

All notable changes to Local Token Saver are documented here. This project
adheres to [Semantic Versioning](https://semver.org/) and the
[Keep a Changelog](https://keepachangelog.com/) format.

## [0.3.0] — pxpipe chain proxy + unified stats

v0.3.0 is cumulative and includes the complete v0.2.0 embedding release.

### Added
- Loopback chain proxy that composes with
  [pxpipe](https://github.com/teamchong/pxpipe): byte-identical `shadow` mode
  (default), opt-in text-only `dedupe`, bounded request framing, true SSE chunk
  streaming, and per-request kill switches (`x-token-saver: off`,
  `TOKEN_SAVER_FILTER=off`).
- Retrieval-tool counterfactuals and correlated token-saver / pxpipe reporting.
  Cache creation and read prices are applied to both pxpipe sides, failed rows
  are excluded, losses stay visible, and shadow-mode wins are reported only as
  projected savings.
- A redacted, preview-only Claude Code wiring command
  (`token-saver mcp install . --with-proxy`) and a dual-health hook template.
  The installer changes no settings, hooks, or live traffic.

## [0.2.0] — embedding-model tier

The zero-dependency hashed-TF vectorizer remains the default; existing indexes
stay valid, and nothing new is installed or downloaded unless you explicitly
opt in.

### Added
- Pluggable embedder backends. `vectors.py` defines an `Embedder` interface with
  the original hashed-TF vectorizer as the default, plus an opt-in ONNX MiniLM
  sentence embedder selectable per workspace via
  `{"embedding": {"backend": "onnx_minilm"}}` in `.tokensaver/config.json`.
- Opt-in setup: `token-saver setup --with-embeddings` installs `onnxruntime` +
  `tokenizers` (also `pip install .[embeddings]`) and downloads the quantized
  model (`Xenova/all-MiniLM-L6-v2`, `model_quantized.onnx`, ~23 MB, Apache-2.0),
  pinned to an exact revision and verified by sha256. This is the only network
  call in the retrieval core; it never runs implicitly, and `--check` never
  downloads.
- Per-backend index and retrieval integration: the indexer records which backend
  built the vectors, switching backends triggers a fast re-embed-only pass
  (chunks and the FTS index are untouched), and retrieval embeds queries with the
  same backend using empirically measured, per-backend score gates.
- Graceful fallback to hashed-TF when the ONNX dependencies or model files are
  absent, with a single stderr warning per process; retrieval never crashes.

### Validation
- Paraphrase recall: on a 50-document corpus with 10 disjoint-vocabulary
  paraphrase queries (no lexical overlap with their targets, so retrieval rests
  entirely on the vector half), `onnx_minilm` returns the correct document at
  rank <= 3 for 7/10 queries versus 2/10 for the default `hashed_tf`.
- Score-gate calibration: the `onnx_minilm` pure-vector gate is set to 0.70,
  below the measured related-pair cosine minima (0.7230 doc-to-doc paraphrase,
  0.7646 query-to-doc) and above the unrelated-pair bulk, so genuinely related
  queries are retained while diverse-unrelated matches are gated out. The gate
  applies only in the pure-vector fallback (when BM25 matches nothing).

## [0.1.0] — initial release

### Added
- Folder-scoped, retrieval-first context access for AI coding and document
  agents, backed by a local `.tokensaver/` index.
- Automatic PDF -> Markdown -> vector pipeline, fully script-based (no LLM).
- Hybrid retrieval: SQLite FTS5 (BM25) + zero-dependency hashed-TF vectors with
  page/line citations back to the original files.
- One-command MCP integration for Claude Code (`.mcp.json`) and Codex
  (`~/.codex/config.toml`).
