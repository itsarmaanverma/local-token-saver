# ADR-0001: Stdlib-only core; `pypdf` as the sole hard dependency

## Status
Accepted

## Context
Local Token Saver installs on end-user machines next to coding agents. Every
dependency is an install-failure surface, a supply-chain risk, and a version
conflict waiting to happen inside users' Python environments. The core loop
(index → retrieve → serve over MCP) must work on a fresh `pip install` with
Python ≥ 3.10, including WSL and distro Pythons.

## Decision
Keep the core on the Python standard library only — `sqlite3` (FTS5), `http`,
`json`, `struct`/`array`, `hashlib` — with `pypdf` as the single hard
dependency (pure-Python PDF text extraction). Everything heavier is an
opt-in extra.

## Consequences

### Positive
- `pip install` succeeds essentially everywhere; no compiler, no wheels drama.
- Zero background services; nothing to keep patched beyond Python itself.
- Config stays JSON (not TOML) so py3.10 needs no `tomli` backport.

### Negative
- Default vector quality is capped (hashed-TF, ADR-0003) until the user opts
  into the ONNX tier.
- Some conveniences (rich CLI output, HTTP/2) are off the table.

### Neutral
- Requires FTS5-enabled sqlite3 — default on Linux/macOS/WSL/python.org
  builds, but a documented hard requirement.

## Alternatives Considered
- **Full deps (fastapi, sentence-transformers, chromadb)** — rejected: ~2 GB
  of transitive installs and CUDA/torch variance for a local CLI tool.
- **Rust/Go binary** — rejected: distribution complexity, contributor
  friction; Python is where the agent-tool ecosystem lives.
