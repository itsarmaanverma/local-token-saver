# Roadmap

Planned and exploratory work for Local Token Saver. Nothing here ships until it
passes explicit quality, economics, and fallback gates, and each new mode lands
off by default with a per-request kill switch.

## Proxy and agent integration
- Live proxy rewiring from `token-saver mcp install`, so the chain proxy can be
  activated directly instead of preview-only.
- A Claude Code PreToolUse hook that routes oversized `Read`/`Grep` calls
  through retrieval automatically.
- An RTK bridge for shell-output compaction.

## PDF page imaging
Render indexed PDF pages as faithful, bounded PNG image blocks (wire-format
compatible with pxpipe), preserving graphs, diagrams, equations, and layout that
text extraction discards. Each image block carries a short text companion with
the source path, page number, and retrieval instructions, and starts in a shadow
mode that records candidate pages and estimated cost without changing requests.

## OCR reflow for scanned documents
An optional local OCR path that extracts embedded text directly for born-digital
PDFs, runs a mobile OCR detector/recognizer on scanned pages, and keeps tables,
charts, diagrams, and low-confidence regions as native crops. Low-confidence
pages always fall back to the full raw page.

## Generative summary tier
An optional generative tiny-LLM tier (for example Qwen2.5-0.5B via llama.cpp)
for chunk contextualization and hierarchical summaries, deferred until the
embedding tier proves out in real use.
