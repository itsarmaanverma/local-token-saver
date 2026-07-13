# Architecture Decision Records

Decisions that shaped Local Token Saver. Format follows the standard ADR
template (Status / Context / Decision / Consequences / Alternatives).

| # | Title | Status |
|---|---|---|
| [0001](0001-stdlib-only-core.md) | Stdlib-only core; `pypdf` as the sole hard dependency | Accepted |
| [0002](0002-sqlite-fts5-index.md) | SQLite + FTS5 as the single index store | Accepted |
| [0003](0003-pluggable-embedder.md) | Pluggable embedder; opt-in ONNX MiniLM INT8 tier | Accepted |
| [0004](0004-mcp-stdio-server.md) | MCP over stdio instead of an HTTP service | Accepted |
| [0005](0005-chain-proxy-shadow-first.md) | Chain proxy with shadow-first mode ladder | Accepted |
| [0006](0006-jsonl-counterfactual-stats.md) | JSONL event logs + counterfactual reporting | Accepted |
| [0007](0007-evidence-framing.md) | Retrieval output framed as untrusted evidence | Accepted |

See [`../ARCHITECTURE.md`](../ARCHITECTURE.md) for the system overview.
