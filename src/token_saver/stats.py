"""JSONL telemetry and cache-aware savings reports for token-saver."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import index_path

CACHE_CREATE_WEIGHT = 1.25
CACHE_CREATE_1H_WEIGHT = 2.0
CACHE_READ_WEIGHT = 0.10
CACHE_TTL_SECONDS = 300
MAX_REPORT_ROWS = 100_000

PRICES: dict[str, float] = {
    "fable": 10.0,
    "opus": 15.0,
    "sonnet": 3.0,
    "haiku": 0.8,
    "default": 3.0,
}


@dataclass
class Event:
    """One token-saver row; optional pxpipe fields keep logs mergeable."""

    ts: int | str
    method: str | None = None
    path: str | None = None
    status: int | None = None
    model: str | None = None
    req_body_sha8: str | None = None
    reason: str | None = None
    mode: str | None = None
    compressed: bool | None = None
    duration_ms: int | None = None
    orig_chars: int | None = None
    filtered_chars: int | None = None
    blocks_deduped: int | None = None
    baseline_tokens: int | None = None
    baseline_cacheable_tokens: int | None = None
    baseline_probe_status: int | str | None = None
    filtered_probe_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_create_tokens: int | None = None
    cache_create_5m_tokens: int | None = None
    cache_create_1h_tokens: int | None = None
    cache_read_tokens: int | None = None
    stop_reason: str | None = None
    first_user_sha8: str | None = None
    system_sha8: str | None = None
    tool: str | None = None
    task_sha8: str | None = None
    returned_tokens: int | None = None
    counterfactual_tokens: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def default_proxy_log_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "token-saver" / "events.jsonl"


def default_pxpipe_log_path() -> Path:
    return Path.home() / ".pxpipe" / "events.jsonl"


def workspace_log_path(root: str | Path) -> Path:
    return Path(root) / ".tokensaver" / "events.jsonl"


def append_event(path: str | Path, event: Event | dict[str, Any]) -> bool:
    """Append a compact event without allowing telemetry to break the caller."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        row = event.to_dict() if isinstance(event, Event) else dict(event)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def iter_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield valid object rows, skipping missing, malformed, or unreadable data."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def load_events(path: str | Path, limit: int = MAX_REPORT_ROWS) -> list[dict[str, Any]]:
    """Read at most the newest ``limit`` rows from an append-only log."""
    return list(deque(iter_events(path), maxlen=max(1, limit)))


def estimate_tokens(text: str) -> int:
    try:
        from .parsers import est_tokens
        return est_tokens(text)
    except Exception:
        return max(1, len(text) // 4)


def _number(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _zero_number(row: dict[str, Any], key: str) -> float:
    value = _number(row, key)
    return value if value is not None else 0.0


def _failed_status(row: dict[str, Any]) -> bool:
    status = _number(row, "status")
    return status is not None and not 200 <= status < 300


def _creation_weight(row: dict[str, Any]) -> float:
    total = _zero_number(row, "cache_create_tokens")
    five = _zero_number(row, "cache_create_5m_tokens")
    hour = _zero_number(row, "cache_create_1h_tokens")
    classified = five + hour
    total = max(total, classified)
    if total <= 0:
        return CACHE_CREATE_WEIGHT
    remainder = max(0.0, total - classified)
    return ((five + remainder) * CACHE_CREATE_WEIGHT
            + hour * CACHE_CREATE_1H_WEIGHT) / total


def effective_input_tokens(row: dict[str, Any]) -> float:
    """Actual billed-equivalent input, including classified 1-hour writes."""
    plain = _zero_number(row, "input_tokens")
    total_create = _zero_number(row, "cache_create_tokens")
    five = _zero_number(row, "cache_create_5m_tokens")
    hour = _zero_number(row, "cache_create_1h_tokens")
    remainder = max(0.0, total_create - five - hour)
    return (
        plain
        + (five + remainder) * CACHE_CREATE_WEIGHT
        + hour * CACHE_CREATE_1H_WEIGHT
        + _zero_number(row, "cache_read_tokens") * CACHE_READ_WEIGHT
    )


def compute_baseline_input_eff(
    baseline: float,
    cacheable: float,
    actual: float,
    warm: bool,
    prev_cacheable: float,
    create_weight: float = CACHE_CREATE_WEIGHT,
) -> float:
    """Port of pxpipe's cache-identical text counterfactual calculation."""
    if baseline <= 0 or cacheable <= 0:
        return actual
    cacheable = min(cacheable, baseline)
    cold_tail = baseline - cacheable
    if not warm:
        return cacheable * create_weight + cold_tail
    reused = min(max(prev_cacheable, 0.0), cacheable)
    grown = cacheable - reused
    return reused * CACHE_READ_WEIGHT + grown * create_weight + cold_tail


def stage(row: dict[str, Any]) -> str:
    if row.get("method"):
        return "proxy"
    if row.get("tool"):
        return "tool"
    return "other"


def rows_by_stage(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"proxy": [], "tool": [], "other": []}
    for row in rows:
        grouped[stage(row)].append(row)
    return grouped


def _token_saver_delta(row: dict[str, Any]) -> float | None:
    baseline = _number(row, "baseline_tokens")
    filtered = _number(row, "filtered_probe_tokens")
    if baseline is None or filtered is None:
        return None
    return baseline - filtered


def summarize_events(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize token-saver proxy and retrieval-tool rows without pxpipe overlap."""
    grouped = rows_by_stage(rows)
    proxy_rows = grouped["proxy"]
    tool_rows = grouped["tool"]
    baseline = actual = saved = projected = 0.0
    measured = 0
    for row in proxy_rows:
        if _failed_status(row):
            continue
        delta = _token_saver_delta(row)
        if delta is None:
            continue
        measured += 1
        base = _zero_number(row, "baseline_tokens")
        if row.get("mode") == "shadow":
            baseline += base
            actual += base
            projected += delta
        elif row.get("mode") in ("dedupe", "retrieve"):
            baseline += base
            actual += _zero_number(row, "filtered_probe_tokens")
            saved += delta

    returned = sum(_zero_number(row, "returned_tokens") for row in tool_rows)
    counterfactual = sum(_zero_number(row, "counterfactual_tokens") for row in tool_rows)
    return {
        "requests": len(proxy_rows),
        "measured_requests": measured,
        "orig_chars": sum(_zero_number(row, "orig_chars") for row in proxy_rows),
        "filtered_chars": sum(_zero_number(row, "filtered_chars") for row in proxy_rows),
        "blocks_deduped": sum(_zero_number(row, "blocks_deduped") for row in proxy_rows),
        "baseline_tokens": baseline,
        "effective_tokens": actual,
        "saved_tokens": saved,
        "projected_saved_tokens": projected,
        "tool_requests": len(tool_rows),
        "returned_tokens": returned,
        "counterfactual_tokens": counterfactual,
        "tool_saved": counterfactual - returned,
    }


def epoch_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _is_anthropic_message(row: dict[str, Any]) -> bool:
    path = row.get("path")
    return isinstance(path, str) and path.rstrip("/").endswith("messages")


def summarize_pxpipe_events(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay pxpipe rows using its observed warm/cold cache accounting."""
    message_rows = [row for row in rows if _is_anthropic_message(row)]
    previous: dict[str, tuple[float, float, str | None]] = {}
    measured = 0
    baseline_total = actual_total = saved_total = dollars = 0.0
    for row in message_rows:
        have_usage = _number(row, "input_tokens") is not None
        actual_eff = effective_input_tokens(row) if have_usage else 0.0
        baseline = _number(row, "baseline_tokens")
        cacheable = _number(row, "baseline_cacheable_tokens") or 0.0
        probe = row.get("baseline_probe_status")
        probe_ok = probe == "ok" or (probe is None and baseline is not None and baseline > 0)
        compressed = row.get("compressed") is True
        credit = (
            not _failed_status(row)
            and compressed and have_usage and probe_ok
            and baseline is not None and baseline > 0
        )

        completion = epoch_seconds(row.get("ts"))
        duration = _zero_number(row, "duration_ms") / 1000.0
        request_start = completion - duration if completion is not None else None
        session = row.get("first_user_sha8")
        prefix = row.get("system_sha8")
        warm = _zero_number(row, "cache_read_tokens") > 0
        prev_cacheable = cacheable
        if warm and isinstance(session, str) and request_start is not None:
            prior = previous.get(session)
            if prior is not None:
                prior_ts, prior_size, prior_prefix = prior
                age = request_start - prior_ts
                same_prefix = prior_prefix is None or prefix is None or prior_prefix == prefix
                if 0 <= age < CACHE_TTL_SECONDS and same_prefix:
                    prev_cacheable = prior_size

        if credit:
            baseline_eff = compute_baseline_input_eff(
                baseline, cacheable, actual_eff, warm, prev_cacheable, _creation_weight(row)
            )
            delta = baseline_eff - actual_eff
            measured += 1
            baseline_total += baseline_eff
            actual_total += actual_eff
            saved_total += delta
            dollars += delta * _price_for_model(row.get("model")) / 1_000_000

        if have_usage and isinstance(session, str) and completion is not None and cacheable > 0:
            previous[session] = (completion, cacheable, prefix if isinstance(prefix, str) else None)

    return {
        "requests": len(message_rows),
        "measured_requests": measured,
        "baseline_tokens": baseline_total,
        "effective_tokens": actual_total,
        "saved_tokens": saved_total,
        "projected_saved_tokens": 0.0,
        "estimated_dollars_saved": dollars,
    }


def _price_for_model(model: str | None) -> float:
    lowered = model.lower() if isinstance(model, str) else ""
    for keyword in ("fable", "opus", "sonnet", "haiku"):
        if keyword in lowered:
            return PRICES[keyword]
    return PRICES["default"]


def est_dollars_saved(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        delta = _token_saver_delta(row)
        if (not _failed_status(row) and delta is not None
                and row.get("mode") in ("dedupe", "retrieve")):
            total += delta * _price_for_model(row.get("model")) / 1_000_000
    return total


def indexed_file_tokens(
    root: str | Path,
    paths: Iterable[str] | None = None,
    folder: str | None = None,
) -> int:
    """Sum indexed full-file tokens without reading source files."""
    database = index_path(Path(root))
    if not database.exists():
        return 0
    try:
        connection = sqlite3.connect(database)
        if paths is not None:
            unique = list(dict.fromkeys(str(path).replace("\\", "/") for path in paths))
            if not unique:
                return 0
            marks = ",".join("?" for _ in unique)
            row = connection.execute(
                f"SELECT COALESCE(SUM(ntokens), 0) FROM files WHERE path IN ({marks})", unique
            ).fetchone()
        else:
            clean = (folder or "").replace("\\", "/").strip("/")
            if clean == ".":
                clean = ""
            elif clean.startswith("./"):
                clean = clean[2:]
            pattern = f"{clean}/%" if clean else "%"
            row = connection.execute(
                "SELECT COALESCE(SUM(ntokens), 0) FROM files WHERE path LIKE ?", (pattern,)
            ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        if "connection" in locals():
            connection.close()


def relevant_paths(result: str) -> list[str]:
    """Extract paths from token-saver's own controlled Relevant files section."""
    paths: list[str] = []
    active = False
    for line in result.splitlines():
        if line.strip() == "Relevant files:":
            active = True
            continue
        if active and line.strip() == "Evidence:":
            break
        if active and line.startswith("- "):
            paths.append(line[2:].strip())
    return paths


def record_tool_event(
    root: str | Path,
    tool: str,
    task: Any,
    result: str,
    *,
    paths: Iterable[str] | None = None,
    folder: str | None = None,
) -> str:
    """Record one retrieval counterfactual and return ``result`` unchanged."""
    cited = list(paths) if paths is not None else relevant_paths(result)
    counterfactual = indexed_file_tokens(root, cited if folder is None else None, folder)
    task_json = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    append_event(
        workspace_log_path(root),
        Event(
            ts=int(datetime.now().timestamp() * 1000),
            tool=tool,
            task_sha8=hashlib.sha256(task_json.encode("utf-8")).hexdigest()[:8],
            returned_tokens=estimate_tokens(result),
            counterfactual_tokens=counterfactual,
        ),
    )
    return result
