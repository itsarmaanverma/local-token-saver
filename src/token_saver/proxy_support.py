"""Pure filtering, usage parsing, and dashboard helpers for the chain proxy."""
from __future__ import annotations

import hashlib
import json
from html import escape

DEDUPE_MIN_CHARS = 2000


def filter_retrieve(body: dict) -> dict:
    """Reserved retrieval hook; v0.3 intentionally performs no extra rewrite."""
    return body


def _eligible_text(block: dict):
    """Return stable text identity and setter for lossless dedupe candidates."""
    content = block.get("content")
    if isinstance(content, str):
        def set_string(stub: str) -> None:
            block["content"] = stub

        return json.dumps(content, ensure_ascii=False), len(content), set_string
    if not isinstance(content, list) or not content:
        return None
    if not all(
        isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and set(item) <= {"type", "text"}
        for item in content
    ):
        return None
    text_chars = sum(len(item["text"]) for item in content)
    identity = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def set_text_list(stub: str) -> None:
        block["content"] = [{"type": "text", "text": stub}]

    return identity, text_chars, set_text_list


def dedupe_body(body: dict, apply: bool) -> tuple[int, int]:
    """Replace later identical, plain-text tool results with stable stubs."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0, 0
    seen: set[str] = set()
    count = saved = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            eligible = _eligible_text(block)
            if eligible is None:
                continue
            identity, text_chars, setter = eligible
            if text_chars <= DEDUPE_MIN_CHARS:
                continue
            sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if sha not in seen:
                seen.add(sha)
                continue
            stub = (
                f"[token-saver: duplicate of sha8={sha[:8]} - identical to an "
                "earlier tool_result in this conversation]"
            )
            count += 1
            saved += max(0, text_chars - len(stub))
            if apply:
                setter(stub)
    return count, saved


def serialize_body(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def merge_usage(obj, usage: dict) -> None:
    if not isinstance(obj, dict):
        return
    data = None
    if isinstance(obj.get("usage"), dict):
        data = obj["usage"]
    elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("usage"), dict):
        data = obj["message"]["usage"]
    if data:
        mapping = {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_creation_input_tokens": "cache_create_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
        }
        for source, target in mapping.items():
            value = data.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[target] = value
        cache = data.get("cache_creation")
        if isinstance(cache, dict):
            five = cache.get("ephemeral_5m_input_tokens")
            hour = cache.get("ephemeral_1h_input_tokens")
            if isinstance(five, int) and five >= 0:
                usage["cache_create_5m_tokens"] = five
            if isinstance(hour, int) and hour >= 0:
                usage["cache_create_1h_tokens"] = hour
    stop_reason = obj.get("stop_reason")
    if stop_reason is None and isinstance(obj.get("delta"), dict):
        stop_reason = obj["delta"].get("stop_reason")
    if stop_reason is None and isinstance(obj.get("message"), dict):
        stop_reason = obj["message"].get("stop_reason")
    if isinstance(stop_reason, str):
        usage["stop_reason"] = stop_reason


def scan_sse(carry: bytearray, chunk: bytes, usage: dict) -> None:
    """Scan complete SSE data lines while bounding an unterminated line."""
    carry.extend(chunk)
    while True:
        newline = carry.find(b"\n")
        if newline < 0:
            if len(carry) > 256 * 1024:
                carry.clear()
            return
        line = bytes(carry[:newline]).strip()
        del carry[: newline + 1]
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            merge_usage(json.loads(payload), usage)
        except (json.JSONDecodeError, ValueError):
            continue


def _summary_row(label: str, summary: dict, chars: str = "-") -> str:
    return (
        "<tr>"
        f"<td>{escape(label)}</td>"
        f"<td>{float(summary.get('requests', 0)):,.0f}</td>"
        f"<td>{escape(chars)}</td>"
        f"<td>{float(summary.get('baseline_tokens', 0)):,.0f}</td>"
        f"<td>{float(summary.get('effective_tokens', 0)):,.0f}</td>"
        f"<td>{float(summary.get('saved_tokens', 0)):,.0f}</td>"
        f"<td>{float(summary.get('projected_saved_tokens', 0)):,.0f}</td>"
        "</tr>"
    )


def dashboard_page(mode: str, upstream: str, log_path: str, token_summary: dict, px_summary: dict) -> bytes:
    filtered = max(
        0,
        int(token_summary.get("orig_chars", 0) - token_summary.get("filtered_chars", 0)),
    )
    rows = "\n".join([
        _summary_row("token-saver", token_summary, f"{filtered:,}"),
        _summary_row("pxpipe", px_summary),
    ])
    page = _DASHBOARD.format(
        mode=escape(mode), upstream=escape(upstream), log=escape(log_path), rows=rows
    )
    return page.encode("utf-8")


_DASHBOARD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>token-saver proxy</title>
<style>
body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;color:#202124;background:#f7f8fa}}
main{{max-width:920px}}h1{{font-size:1.25rem;margin:0 0 .25rem}}
.meta{{color:#5f6368;margin-bottom:1rem;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #d9dce1;padding:.45rem .6rem;text-align:right}}
th:first-child,td:first-child{{text-align:left}}thead th{{background:#eef1f4}}
@media(max-width:680px){{body{{margin:1rem}}table{{font-size:12px}}th,td{{padding:.35rem}}}}
</style><main><h1>token-saver proxy</h1>
<div class="meta">mode <b>{mode}</b> | upstream <code>{upstream}</code> | log <code>{log}</code></div>
<table><thead><tr><th>stage</th><th>requests</th><th>candidate chars</th>
<th>baseline</th><th>sent</th><th>saved</th><th>projected</th></tr></thead>
<tbody>{rows}</tbody></table></main>"""
