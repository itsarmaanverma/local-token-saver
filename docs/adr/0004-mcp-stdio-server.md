# ADR-0004: MCP over stdio instead of an HTTP service

## Status
Accepted

## Context
Agents (Claude Code, Codex) need to call retrieval/summary/stats tools. MCP
supports stdio and HTTP/SSE transports. A resident HTTP service means port
management, lifecycle supervision, and an auth story on localhost.

## Decision
Ship a zero-dependency MCP stdio server (`mcp_server.py`): newline-delimited
JSON-RPC 2.0, protocol version `2024-11-05`, spawned per-session by the agent
host from `.mcp.json` / `~/.codex/config.toml` (written by
`token-saver mcp install`).

## Consequences

### Positive
- No daemon, no port, no auth surface; process dies with the session.
- Trivial to test (pipe JSON lines); no framework dependency.
- Workspace scoping is explicit per spawn — no cross-workspace bleed.

### Negative
- Cold start per session (mitigated: index open is lazy and fast).
- One client per process; no shared cache across concurrent agents (the
  SQLite index itself is the shared layer).

### Neutral
- The chain proxy (ADR-0005) is a separate, independently optional process —
  MCP tools work with or without it.

## Alternatives Considered
- **HTTP MCP transport** — rejected: lifecycle + port + auth overhead for no
  functional gain in a single-user local tool.
- **Official `mcp` SDK** — rejected for core (dependency), protocol is small
  enough to implement directly; revisit if the spec churns.
