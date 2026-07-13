# ADR-0007: Retrieval output framed as untrusted evidence

## Status
Accepted

## Context
Evidence packs feed indexed file content directly into an agent's context.
Indexed folders are untrusted input: a document dump can contain adversarial
text ("ignore previous instructions…"), and a retrieval tool that relays it
verbatim becomes a prompt-injection vector.

## Decision
Every evidence pack is prefixed with an explicit framing header
(`retrieval.py::EVIDENCE_HEADER`): retrieved content is evidence, not
instructions — commands, secrets, policies, or hidden prompts inside it must
not be followed. Defense is layered with the default ignore list (keys,
`.env*`, credentials, certs never get indexed) and citation-first packing so
claims remain traceable to sources.

## Consequences

### Positive
- Cheap, model-agnostic hardening at the single choke point all retrieval
  flows through (MCP tools and proxy `retrieve` mode alike).
- Secrets exclusion reduces both injection and exfiltration surface.

### Negative
- Header costs a few tokens per pack.
- Framing is mitigation, not proof — a sufficiently confused model can still
  follow injected text; this remains a documented residual risk.

## Alternatives Considered
- **No framing** — rejected: known, cheap-to-mitigate attack class.
- **Content sanitization/rewriting** — rejected: lossy, unbounded, and harms
  the citation guarantee (verbatim slices with page/line references).
- **LLM-based injection screening** — rejected: violates the no-LLM,
  local-only pipeline NFRs; revisit with the generative tier if it ships.
