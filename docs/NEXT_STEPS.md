# Deferred proxy validation and PDF imaging

These items are plans, not v0.3 features. Do not persist the proxy URL or enable
active filtering until the shadow-mode smoke gate below passes.

## 1. Live shadow smoke gate

Run this in a throwaway shell after installing the v0.3 package. It changes no
Claude settings and leaves `TOKEN_SAVER_FILTER=shadow`, so the request body sent
to pxpipe remains byte-identical.

```bash
# Terminal 1: pxpipe, unchanged
npx -y pxpipe-proxy

# Terminal 2: token-saver in front of pxpipe
TOKEN_SAVER_FILTER=shadow \
TOKEN_SAVER_UPSTREAM=http://127.0.0.1:47821 \
token-saver-proxy

# Terminal 3: health and a session-scoped Claude test
curl -fsS http://127.0.0.1:47820/health
ANTHROPIC_BASE_URL=http://127.0.0.1:47820 claude -p "Reply with OK." < /dev/null
token-saver stats .
```

Pass criteria:

1. The Claude request completes normally and streaming starts without waiting
   for the response to close.
2. One corresponding row appears in both
   `~/.local/state/token-saver/events.jsonl` and `~/.pxpipe/events.jsonl`.
3. `token-saver stats` reports the pxpipe row as matched (exact hash for
   passthrough or one unambiguous bounded model/time candidate for pxpipe's transformed hash), keeps token-saver
   savings at zero, and shows any shadow candidate only in `projected`.
4. `ANTHROPIC_BASE_URL` remains unchanged in `~/.claude/settings.json`.

After that gate, inspect `token-saver mcp install . --with-proxy`. It is a
preview only. Apply its targeted URL and hook changes manually, then repeat the
same gate before considering `TOKEN_SAVER_FILTER=dedupe`.

## 2. PDF pages as image blocks

The safest first implementation is a faithful page raster, not OCR reflow:

```text
indexed PDF page citation
        |
        v
deterministic PDF rasterizer -> bounded PNG page -> Anthropic image block
        |                                           + short text citation
        v
newest-turn replacement only; prior cached turns remain unchanged
```

Design constraints:

- Add an optional PDF extra such as
  [pypdfium2](https://github.com/pypdfium2-team/pypdfium2); keep the current
  pypdf text index as the retrieval source.
- Emit pxpipe-compatible Anthropic wire blocks:
  `{type: "image", source: {type: "base64", media_type: "image/png", data: ...}}`.
  Reuse pxpipe's current transform contract rather than inventing another
  message shape. See [pxpipe transform source](https://github.com/teamchong/pxpipe/blob/main/src/core/transform.ts).
- Preserve page aspect ratio and the full page. A native page raster retains
  graphs, diagrams, equations, and layout that text extraction discards.
- Include a small text companion containing the source path, page number, and
  `get_source_slice`/retrieval instructions. Exact identifiers should remain
  text when available because image reading is not byte-exact.
- Cap pages, decoded pixels, encoded bytes, render time, and total image blocks.
  Reject encrypted or malformed PDFs cleanly.
- Cache by PDF sha256, page, DPI, color mode, encoder version, and target model.
  Identical inputs must produce identical PNG bytes for prompt-cache stability.
- Start in a PDF shadow mode that records candidate pages and estimated image
  cost without changing requests.

The target is wire-format compatibility with pxpipe. Pixel-identical output is
not promised: portrait PDF pages and pxpipe's dense text pages have different
geometry and should not be distorted to match.

## 3. Experimental tiny OCR reflow

OCR reflow is useful for scanned documents, but it cannot guarantee both tiny
runtime cost and lossless document understanding. Use this hierarchy:

1. Born-digital PDF: extract embedded text directly. OCR would be slower and
   less accurate.
2. Scanned page: run a mobile OCR detector/recognizer, initially
   [PP-OCRv5 mobile](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5.md),
   behind an optional local runtime.
3. Reflow high-confidence text through pxpipe's
   [`renderTextToImages`](https://github.com/teamchong/pxpipe#library-use-no-proxy)
   so text pages use the same renderer and image-block contract.
4. Keep tables, charts, diagrams, photos, equations, handwriting, and all
   low-confidence regions as native page or region crops alongside the reflowed
   text. Never ask the OCR model to recreate visual information.
5. Fall back to the full raw page whenever confidence, layout coverage, or crop
   accounting is incomplete.

PP-OCR's mobile recognizers are small, but the complete detector, recognizer,
runtime, and layout stack is larger than a single model file. Model size alone
must not be used as the acceptance criterion.

## 4. Required quality gates

Build a fixed evaluation corpus containing born-digital and scanned pages,
multi-column layouts, tables, charts, equations, code, small fonts, rotated
pages, and exact IDs. Do not ship active replacement until all gates pass:

- Page accounting: every requested page and every non-text region is present.
- OCR: character/word error plus a separate exact-ID score; no silent invented
  characters.
- Visual QA: questions over graphs, tables, and diagrams match raw-page answers.
- Determinism: repeated conversion produces byte-identical PNGs and message JSON.
- Economics: measured `count_tokens` baseline beats image cost after cache
  creation/read pricing, including output in the honest denominator.
- Operations: bounded CPU, RAM, latency, file size, pages, and timeout behavior.
- Fallback: low confidence always selects the raw page, never partial reflow.

Suggested delivery order: direct page raster in shadow mode, page-raster active
allowlist, OCR shadow evaluation, then OCR active allowlist. Keep a per-request
kill switch and leave every new mode off by default.
