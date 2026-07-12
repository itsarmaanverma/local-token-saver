"""Merged report construction and rendering for token-saver statistics."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .stats import (
    PRICES,
    default_proxy_log_path,
    default_pxpipe_log_path,
    epoch_seconds,
    est_dollars_saved,
    load_events,
    summarize_events,
    summarize_pxpipe_events,
    workspace_log_path,
)


def correlate_pxpipe(
    proxy_rows: list[dict[str, Any]],
    pxpipe_rows: list[dict[str, Any]],
    window: float = 30.0,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Match exact passthrough hashes or transformed pxpipe rows by model/time."""
    groups: dict[str, list[tuple[int, dict[str, Any], float | None]]] = {}
    for index, row in enumerate(pxpipe_rows):
        model = str(row.get("model") or "")
        groups.setdefault(model, []).append((index, row, epoch_seconds(row.get("ts"))))
    used: set[int] = set()
    matched: list[dict[str, Any]] = []
    exact_count = transformed_count = ambiguous_count = 0
    for proxy in proxy_rows:
        sha = proxy.get("req_body_sha8")
        model = str(proxy.get("model") or "")
        timestamp = epoch_seconds(proxy.get("ts"))
        choices = [item for item in groups.get(model, []) if item[0] not in used]
        if not choices:
            continue
        timed = [
            item for item in choices
            if timestamp is not None and item[2] is not None
            and abs(timestamp - item[2]) <= window
        ]
        exact = [item for item in timed if isinstance(sha, str)
                 and item[1].get("req_body_sha8") == sha]
        if exact:
            selected = min(exact, key=lambda item: abs(timestamp - item[2]))
            exact_count += 1
        else:
            transformed = [item for item in timed if item[1].get("compressed") is True]
            if len(transformed) > 1:
                ambiguous_count += 1
                continue
            if not transformed or not model:
                continue
            selected = transformed[0]
            transformed_count += 1
        used.add(selected[0])
        matched.append(selected[1])
    return matched, exact_count, transformed_count, ambiguous_count


def build_report(
    root: str | Path,
    proxy_log: str | Path | None = None,
    pxpipe_log: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(root)
    proxy_path = Path(proxy_log) if proxy_log else default_proxy_log_path()
    pxpipe_path = Path(pxpipe_log) if pxpipe_log else default_pxpipe_log_path()
    workspace_rows = load_events(workspace_log_path(root))
    proxy_rows = load_events(proxy_path)
    all_pxpipe_rows = load_events(pxpipe_path)
    matched_pxpipe, exact_matches, transformed_matches, ambiguous_matches = correlate_pxpipe(
        proxy_rows, all_pxpipe_rows
    )
    pxpipe_rows = matched_pxpipe if proxy_rows else all_pxpipe_rows
    workspace = summarize_events(workspace_rows)
    proxy = summarize_events(proxy_rows)
    pxpipe = summarize_pxpipe_events(pxpipe_rows)
    tools = {
        "requests": workspace["tool_requests"],
        "baseline_tokens": workspace["counterfactual_tokens"],
        "effective_tokens": workspace["returned_tokens"],
        "saved_tokens": workspace["tool_saved"],
        "projected_saved_tokens": 0.0,
        "estimated_dollars_saved": workspace["tool_saved"] * PRICES["default"] / 1_000_000,
    }
    proxy["estimated_dollars_saved"] = est_dollars_saved(proxy_rows)
    stages = {"retrieval tools": tools, "token-saver proxy": proxy, "pxpipe": pxpipe}
    combined = {
        key: sum(float(row.get(key, 0) or 0) for row in stages.values())
        for key in (
            "requests", "baseline_tokens", "effective_tokens", "saved_tokens",
            "projected_saved_tokens", "estimated_dollars_saved",
        )
    }
    return {
        "stages": stages,
        "combined": combined,
        "sources": {
            "workspace": str(workspace_log_path(root)),
            "proxy": str(proxy_path),
            "pxpipe": str(pxpipe_path),
            "pxpipe_rows": len(all_pxpipe_rows),
            "pxpipe_rows_matched": len(matched_pxpipe),
            "pxpipe_exact_hash_matches": exact_matches,
            "pxpipe_transformed_time_matches": transformed_matches,
            "pxpipe_ambiguous_time_matches_skipped": ambiguous_matches,
            "pxpipe_scope": "matched" if proxy_rows else "all",
        },
    }


def format_report(report: dict[str, Any]) -> str:
    headings = ("stage", "calls", "baseline", "sent", "saved", "projected", "est. USD")
    lines = [
        f"{headings[0]:<20} {headings[1]:>7} {headings[2]:>13} {headings[3]:>13} "
        f"{headings[4]:>13} {headings[5]:>13} {headings[6]:>10}",
        "-" * 96,
    ]
    for label, row in [*report["stages"].items(), ("combined", report["combined"])]:
        lines.append(
            f"{label:<20} {float(row.get('requests', 0)):>7,.0f} "
            f"{float(row.get('baseline_tokens', 0)):>13,.0f} "
            f"{float(row.get('effective_tokens', 0)):>13,.0f} "
            f"{float(row.get('saved_tokens', 0)):>13,.0f} "
            f"{float(row.get('projected_saved_tokens', 0)):>13,.0f} "
            f"${float(row.get('estimated_dollars_saved', 0)):>9,.4f}"
        )
    source = report["sources"]
    lines.extend([
        "",
        f"pxpipe scope: {source['pxpipe_scope']} "
        f"({source['pxpipe_rows_matched']} matched of {source['pxpipe_rows']} retained rows)",
        f"match method: {source['pxpipe_exact_hash_matches']} exact hash, "
        f"{source['pxpipe_transformed_time_matches']} compressed model/time, "
        f"{source['pxpipe_ambiguous_time_matches_skipped']} ambiguous skipped",
        "Dollar values are input-side estimates; negative savings are preserved.",
    ])
    return "\n".join(lines)
