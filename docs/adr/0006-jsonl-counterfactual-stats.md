# ADR-0006: JSONL event logs + counterfactual savings reporting

## Status
Accepted

## Context
The product's claim is "saves tokens/money." That claim needs honest local
measurement across two cooperating stages (token-saver, pxpipe) without
shipping telemetry off-machine, and without a database dependency for what is
fundamentally an append-only event stream.

## Decision
Append-only JSONL event logs (tool events, proxy events, pxpipe events) under
the local state dir, correlated at report time (`stats_report.py`). Reporting
rules: cache creation/read prices applied to both pxpipe sides, failed rows
excluded, losses stay visible, shadow-mode wins reported only as projected,
and retrieval-tool *counterfactuals* estimate what a raw read would have cost.

## Consequences

### Positive
- Crash-safe appends; logs are greppable, truncatable, and diffable.
- Honest accounting (losses visible, projections labeled) protects trust in
  the headline savings numbers.
- No schema migrations for an evolving event shape.

### Negative
- Report-time correlation is O(events); very long-lived logs need rotation.
- Counterfactuals are estimates, not ground truth — labeled as such.

## Alternatives Considered
- **SQLite for events** — rejected: write contention with the indexer, and
  JSONL's append/rotate ergonomics fit better; the index DB stays query-only.
- **Live A/B measurement** — rejected: would require applying transforms to
  live traffic to measure them (contradicts ADR-0005 shadow-first).
