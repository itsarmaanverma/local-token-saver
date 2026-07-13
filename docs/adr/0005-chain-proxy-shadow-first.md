# ADR-0005: Chain proxy with shadow-first mode ladder + kill switches

## Status
Accepted

## Context
Beyond MCP tools, token savings can come from filtering repeated tool results
in live API traffic before pxpipe renders dense context as images. But a proxy
that rewrites live agent requests can silently corrupt sessions — the worst
possible failure for a trust-sensitive tool.

## Decision
A loopback chain proxy (`:47820`, ThreadingHTTPServer) composing with pxpipe
(`:47821`) upstream, with a strict mode ladder:

`off` → `shadow` (default: byte-identical passthrough, savings *measured but
not applied*) → `dedupe` (text-only repeated-tool-result dedup) → `retrieve`.

Every transforming mode has per-request kill switches (`x-token-saver: off`
header, `TOKEN_SAVER_FILTER=off` env). Bodies are capped (128 MB), SSE is
streamed chunk-true, and `mcp install --with-proxy` is preview-only — it
changes no settings, hooks, or live traffic.

## Consequences

### Positive
- New filters earn trust with real traffic data before touching a single
  request; shadow-mode wins are reported only as *projected* savings.
- Users can bail out per-request without restarting anything.
- Composition keeps pxpipe untouched — each stage owns one concern.

### Negative
- Two loopback hops add (small) latency and two processes to health-check.
- Shadow mode doubles body handling work (hash + measure + passthrough).

## Alternatives Considered
- **Fork/patch pxpipe** — rejected: divergence cost; composition via
  `ANTHROPIC_BASE_URL` chaining is the designed extension point.
- **Rewrite-by-default** — rejected: violates the safety NFR; opt-in ladder
  matches ROADMAP's "off by default with per-request kill switch" gate.
- **MCP-only (no proxy)** — kept as the default posture; the proxy remains
  strictly optional.
