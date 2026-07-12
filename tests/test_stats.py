"""Tests for token_saver.stats: JSONL event log + savings aggregation."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from token_saver.parsers import est_tokens
from token_saver.stats import (
    Event,
    append_event,
    compute_baseline_input_eff,
    default_proxy_log_path,
    effective_input_tokens,
    est_dollars_saved,
    estimate_tokens,
    iter_events,
    rows_by_stage,
    stage,
    summarize_events,
    summarize_pxpipe_events,
    workspace_log_path,
)
from token_saver.stats_report import build_report

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_workspace_log_path(tmp_path):
    assert workspace_log_path(tmp_path) == tmp_path / ".tokensaver" / "events.jsonl"
    # str root is accepted too
    assert workspace_log_path(str(tmp_path)) == tmp_path / ".tokensaver" / "events.jsonl"


def test_default_proxy_log_path_uses_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_proxy_log_path() == tmp_path / "token-saver" / "events.jsonl"


def test_default_proxy_log_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path.home() / ".local" / "state" / "token-saver" / "events.jsonl"
    assert default_proxy_log_path() == expected


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_matches_parsers_est_tokens():
    for text in ("", "a", "hello world", "x" * 401):
        assert estimate_tokens(text) == est_tokens(text)


def test_estimate_tokens_basic():
    assert estimate_tokens("") == 1  # max(1, 0 // 4)
    assert estimate_tokens("a" * 40) == 10  # 40 // 4


# ---------------------------------------------------------------------------
# append_event / iter_events roundtrip
# ---------------------------------------------------------------------------


def test_append_and_iter_roundtrip_including_corrupt_line(tmp_path):
    log = tmp_path / "events.jsonl"

    event1 = Event(
        ts=1111, method="POST", path="/v1/messages", status=200,
        model="claude-sonnet-4-20250514", input_tokens=5,
    )
    assert append_event(log, event1) is True

    event2_dict = {"ts": 2222, "tool": "search", "returned_tokens": 3}
    assert append_event(log, event2_dict) is True

    # Simulate external corruption: a non-JSON line and a blank line.
    with open(log, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write("\n")

    event3_dict = {"ts": 3333, "method": "GET", "path": "/health", "status": 200}
    assert append_event(log, event3_dict) is True

    rows = list(iter_events(log))
    assert len(rows) == 3  # corrupt + blank line silently skipped

    # Event.to_dict() drops None fields, so only explicitly-set keys appear.
    assert rows[0] == {
        "ts": 1111, "method": "POST", "path": "/v1/messages", "status": 200,
        "model": "claude-sonnet-4-20250514", "input_tokens": 5,
    }
    assert rows[1] == event2_dict
    assert rows[2] == event3_dict

    # Log file is one compact JSON object per line.
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5  # 3 valid + 1 corrupt + 1 blank
    json.loads(lines[0])  # first line is valid JSON


def test_iter_events_tolerates_missing_file(tmp_path):
    assert list(iter_events(tmp_path / "does-not-exist.jsonl")) == []


def test_append_event_never_raises_on_bad_path(tmp_path):
    # tmp_path/"blocker" is a regular file, so treating it as a parent
    # directory must fail -- append_event should swallow the error.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    ok = append_event(blocker / "events.jsonl", Event(ts=1))
    assert ok is False


# ---------------------------------------------------------------------------
# effective_input_tokens
# ---------------------------------------------------------------------------


def test_effective_input_tokens_weights_cache_correctly():
    row = {"input_tokens": 10, "cache_create_tokens": 4, "cache_read_tokens": 20}
    # 10 + 1.25*4 + 0.1*20 = 10 + 5 + 2 = 17
    assert effective_input_tokens(row) == pytest.approx(17.0)


def test_effective_input_tokens_missing_fields_default_to_zero():
    assert effective_input_tokens({}) == 0
    assert effective_input_tokens({"input_tokens": 7}) == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# stage / rows_by_stage
# ---------------------------------------------------------------------------


def test_stage_classification():
    assert stage({"method": "POST"}) == "proxy"
    assert stage({"tool": "search"}) == "tool"
    assert stage({}) == "other"
    # method takes precedence when both are (unexpectedly) present
    assert stage({"method": "POST", "tool": "search"}) == "proxy"


def test_rows_by_stage_groups_correctly():
    rows = [{"method": "POST"}, {"tool": "search"}, {"status": 200}]
    grouped = rows_by_stage(rows)
    assert grouped["proxy"] == [rows[0]]
    assert grouped["tool"] == [rows[1]]
    assert grouped["other"] == [rows[2]]


def _fixture_rows():
    row_a = {
        "ts": 1000, "method": "POST", "path": "/v1/messages", "status": 200,
        "model": "claude-sonnet-4-20250514", "req_body_sha8": "abc12345",
        "orig_chars": 4000, "filtered_chars": 1000, "blocks_deduped": 1,
        "baseline_tokens": 1000, "filtered_probe_tokens": 300,
        "mode": "dedupe",
    }
    row_b = {
        "ts": 2000, "method": "POST", "path": "/v1/messages", "status": 200,
        "model": "claude-3-5-haiku-20241022",
        "orig_chars": 2000, "filtered_chars": 1500, "blocks_deduped": 1,
        "baseline_tokens": 500, "filtered_probe_tokens": 400,
        "mode": "shadow",
    }
    row_c = {"ts": 2500, "method": "GET", "path": "/health", "status": 200}
    row_d = {
        "ts": 3000, "tool": "retrieve_context", "task_sha8": "deadbeef1",
        "returned_tokens": 150, "counterfactual_tokens": 900,
    }
    return [row_a, row_b, row_c, row_d]


def test_summarize_events_matches_hand_computed_values():
    summary = summarize_events(_fixture_rows())

    assert summary["requests"] == 3  # rows A, B, C (proxy stage)
    assert summary["orig_chars"] == 6000  # 4000 + 2000 + 0
    assert summary["filtered_chars"] == 2500
    assert summary["blocks_deduped"] == 2
    assert summary["baseline_tokens"] == 1500
    assert summary["effective_tokens"] == 800
    assert summary["saved_tokens"] == 700
    assert summary["projected_saved_tokens"] == 100

    assert summary["tool_requests"] == 1
    assert summary["returned_tokens"] == 150
    assert summary["counterfactual_tokens"] == 900
    assert summary["tool_saved"] == 750


def test_summarize_events_empty_input():
    summary = summarize_events([])
    assert summary["requests"] == 0
    assert summary["orig_chars"] == 0
    assert summary["saved_tokens"] == 0
    assert summary["tool_saved"] == 0


def test_failed_proxy_rows_are_not_credited():
    row = {
        "method": "POST", "path": "/v1/messages", "status": 500,
        "mode": "dedupe", "model": "claude-sonnet-4",
        "baseline_tokens": 100, "filtered_probe_tokens": 40,
    }
    summary = summarize_events([row])
    assert summary["requests"] == 1
    assert summary["measured_requests"] == 0
    assert summary["saved_tokens"] == 0
    assert est_dollars_saved([row]) == 0


def test_est_dollars_saved_matches_hand_computed_value():
    expected = 700 * 3.0 / 1_000_000
    total = est_dollars_saved(_fixture_rows())
    assert total == pytest.approx(expected)
    assert math.isclose(total, 0.0021, rel_tol=1e-9)


def test_est_dollars_saved_uses_default_price_for_unknown_model():
    rows = [{"method": "POST", "model": "some-other-vendor-model", "mode": "dedupe",
             "baseline_tokens": 100, "filtered_probe_tokens": 0}]
    assert est_dollars_saved(rows) == pytest.approx(100 * 3.0 / 1_000_000)


def test_est_dollars_saved_no_baseline_rows_is_zero():
    rows = [{"method": "GET", "path": "/health", "status": 200}]
    assert est_dollars_saved(rows) == 0.0


def test_effective_input_tokens_prices_one_hour_cache_writes():
    row = {
        "input_tokens": 10,
        "cache_create_tokens": 6,
        "cache_create_5m_tokens": 2,
        "cache_create_1h_tokens": 4,
    }
    assert effective_input_tokens(row) == pytest.approx(20.5)


def test_compute_baseline_input_eff_warm_and_cold():
    actual = 225
    assert compute_baseline_input_eff(1000, 800, actual, False, 0) == 1200
    assert compute_baseline_input_eff(1000, 800, actual, True, 600) == 510


def test_pxpipe_summary_is_cache_aware_and_keeps_losses():
    rows = [
        {
            "ts": "2026-01-01T00:00:01Z", "duration_ms": 100,
            "method": "POST", "path": "/v1/messages", "status": 200,
            "compressed": True, "baseline_probe_status": "ok",
            "baseline_tokens": 1000, "baseline_cacheable_tokens": 800,
            "input_tokens": 100, "cache_create_tokens": 100,
            "cache_read_tokens": 0, "first_user_sha8": "session",
            "system_sha8": "prefix", "model": "claude-sonnet-4",
        },
        {
            "ts": "2026-01-01T00:00:02Z", "duration_ms": 100,
            "method": "POST", "path": "/v1/messages", "status": 200,
            "compressed": True, "baseline_probe_status": "ok",
            "baseline_tokens": 1000, "baseline_cacheable_tokens": 900,
            "input_tokens": 700, "cache_create_tokens": 300,
            "cache_read_tokens": 100, "first_user_sha8": "session",
            "system_sha8": "prefix", "model": "claude-sonnet-4",
        },
        {
            "ts": "2026-01-01T00:00:03Z", "method": "POST",
            "path": "/v1/messages", "status": 429, "compressed": True,
            "baseline_probe_status": "ok", "baseline_tokens": 9999,
            "baseline_cacheable_tokens": 900,
        },
    ]
    summary = summarize_pxpipe_events(rows)
    # Cold: 1200 - 225 = 975. Warm with prior 800: 80 + 125 + 100 - 1085 = -780.
    assert summary["measured_requests"] == 2
    assert summary["saved_tokens"] == pytest.approx(195)


def test_failed_pxpipe_rows_are_not_credited_even_with_usage():
    row = {
        "method": "POST", "path": "/v1/messages", "status": 429,
        "compressed": True, "baseline_probe_status": "ok",
        "baseline_tokens": 100, "baseline_cacheable_tokens": 100,
        "input_tokens": 10, "cache_create_tokens": 0,
    }
    summary = summarize_pxpipe_events([row])
    assert summary["requests"] == 1
    assert summary["measured_requests"] == 0
    assert summary["saved_tokens"] == 0


def test_pxpipe_summary_ignores_uncompressed_and_malformed_values():
    rows = [
        {"method": "POST", "path": "/v1/messages", "compressed": False,
         "baseline_tokens": 1000, "input_tokens": 5},
        {"method": "POST", "path": "/v1/messages", "compressed": True,
         "baseline_probe_status": "ok", "baseline_tokens": "huge",
         "input_tokens": True, "cache_read_tokens": []},
    ]
    summary = summarize_pxpipe_events(rows)
    assert summary["measured_requests"] == 0
    assert summary["saved_tokens"] == 0


def test_pxpipe_summary_prices_classified_one_hour_cache_writes_on_both_sides():
    row = {
        "ts": 1000, "method": "POST", "path": "/v1/messages", "status": 200,
        "compressed": True, "baseline_probe_status": "ok",
        "baseline_tokens": 100, "baseline_cacheable_tokens": 100,
        "input_tokens": 0, "cache_create_tokens": 6,
        "cache_create_1h_tokens": 4, "cache_read_tokens": 0,
    }
    summary = summarize_pxpipe_events([row])
    # Two unclassified writes at 1.25x plus four 1h writes at 2x => 1.75x baseline.
    assert summary["baseline_tokens"] == pytest.approx(175)
    assert summary["effective_tokens"] == pytest.approx(10.5)
    assert summary["saved_tokens"] == pytest.approx(164.5)


def test_build_report_separates_shadow_projection_and_pxpipe_stage(tmp_path):
    root = tmp_path / "workspace"
    (root / ".tokensaver").mkdir(parents=True)
    proxy_log = tmp_path / "proxy.jsonl"
    pxpipe_log = tmp_path / "pxpipe.jsonl"
    append_event(proxy_log, {
        "ts": 2000, "method": "POST", "path": "/v1/messages", "status": 200,
        "mode": "shadow", "model": "claude-sonnet-4", "req_body_sha8": "feedbeef",
        "baseline_tokens": 1000, "filtered_probe_tokens": 800,
    })
    append_event(pxpipe_log, {
        "ts": 1990, "method": "POST", "path": "/v1/messages", "status": 200,
        "model": "claude-sonnet-4", "req_body_sha8": "feedbeef", "compressed": True,
        "baseline_probe_status": "ok", "baseline_tokens": 1000,
        "baseline_cacheable_tokens": 800, "input_tokens": 500,
    })
    report = build_report(root, proxy_log=proxy_log, pxpipe_log=pxpipe_log)
    token_stage = report["stages"]["token-saver proxy"]
    assert token_stage["saved_tokens"] == 0
    assert token_stage["projected_saved_tokens"] == 200
    assert report["stages"]["pxpipe"]["saved_tokens"] == pytest.approx(700)
    assert report["combined"]["saved_tokens"] == pytest.approx(700)
    assert report["sources"]["pxpipe_rows_matched"] == 1
    assert report["sources"]["pxpipe_exact_hash_matches"] == 1


def test_build_report_correlates_pxpipe_transformed_hash_by_model_and_time(tmp_path):
    root = tmp_path / "workspace"
    (root / ".tokensaver").mkdir(parents=True)
    proxy_log = tmp_path / "proxy.jsonl"
    pxpipe_log = tmp_path / "pxpipe.jsonl"
    append_event(proxy_log, {
        "ts": 20_000, "method": "POST", "path": "/v1/messages", "status": 200,
        "mode": "dedupe", "model": "claude-fable-5", "req_body_sha8": "incoming",
        "baseline_tokens": 1000, "filtered_probe_tokens": 800,
    })
    append_event(pxpipe_log, {
        "ts": 19_990, "method": "POST", "path": "/v1/messages", "status": 200,
        "model": "claude-fable-5", "req_body_sha8": "transformed", "compressed": True,
        "baseline_probe_status": "ok", "baseline_tokens": 800,
        "baseline_cacheable_tokens": 600, "input_tokens": 300,
    })
    report = build_report(root, proxy_log=proxy_log, pxpipe_log=pxpipe_log)
    assert report["sources"]["pxpipe_rows_matched"] == 1
    assert report["sources"]["pxpipe_exact_hash_matches"] == 0
    assert report["sources"]["pxpipe_transformed_time_matches"] == 1


def test_build_report_skips_ambiguous_compressed_time_matches(tmp_path):
    root = tmp_path / "workspace"
    (root / ".tokensaver").mkdir(parents=True)
    proxy_log = tmp_path / "proxy.jsonl"
    pxpipe_log = tmp_path / "pxpipe.jsonl"
    append_event(proxy_log, {
        "ts": 20_000, "method": "POST", "path": "/v1/messages", "status": 200,
        "mode": "dedupe", "model": "claude-fable-5", "req_body_sha8": "incoming",
    })
    for timestamp in (19_990, 20_010):
        append_event(pxpipe_log, {
            "ts": timestamp, "method": "POST", "path": "/v1/messages", "status": 200,
            "model": "claude-fable-5", "req_body_sha8": str(timestamp), "compressed": True,
        })
    report = build_report(root, proxy_log=proxy_log, pxpipe_log=pxpipe_log)
    assert report["sources"]["pxpipe_rows_matched"] == 0
    assert report["sources"]["pxpipe_ambiguous_time_matches_skipped"] == 1
