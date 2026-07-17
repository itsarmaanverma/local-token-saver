"""Repeatable performance probes for phased efficiency work.

Run from an installed checkout, for example:

    python scripts/benchmark_efficiency.py stats --sizes 1000,10000,100000
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

from token_saver.stats_report import correlate_pxpipe


def _stats_rows(count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create chronological, uniquely hash-matched proxy/pxpipe rows."""
    start_ms = 1_700_000_000_000
    proxy_rows: list[dict[str, Any]] = []
    pxpipe_rows: list[dict[str, Any]] = []
    for index in range(count):
        timestamp = start_ms + index * 1000
        digest = f"{index:08x}"
        proxy_rows.append({
            "ts": timestamp,
            "model": "claude-sonnet-benchmark",
            "req_body_sha8": digest,
        })
        pxpipe_rows.append({
            "ts": timestamp,
            "model": "claude-sonnet-benchmark",
            "req_body_sha8": digest,
            "compressed": True,
        })
    return proxy_rows, pxpipe_rows


def benchmark_stats(count: int, repeat: int) -> dict[str, int | float]:
    proxy_rows, pxpipe_rows = _stats_rows(count)
    timings = []
    for _ in range(repeat):
        started = time.perf_counter()
        matched, exact, transformed, ambiguous = correlate_pxpipe(proxy_rows, pxpipe_rows)
        timings.append(time.perf_counter() - started)
        if (len(matched), exact, transformed, ambiguous) != (count, count, 0, 0):
            raise RuntimeError("correlation benchmark produced incorrect match counts")
    return {
        "rows_per_side": count,
        "median_seconds": round(statistics.median(timings), 6),
        "repeat": repeat,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="case", required=True)
    stats = subparsers.add_parser("stats", help="benchmark pxpipe correlation")
    stats.add_argument("--sizes", default="1000,10000,100000")
    stats.add_argument("--repeat", type=_positive_int, default=1)
    args = parser.parse_args()

    if args.case == "stats":
        sizes = [_positive_int(value.strip()) for value in args.sizes.split(",")]
        result = {
            "case": "stats_correlation",
            "results": [benchmark_stats(size, args.repeat) for size in sizes],
        }
        print(json.dumps(result, indent=2))
        return 0
    raise AssertionError(f"unhandled benchmark case: {args.case}")


if __name__ == "__main__":
    raise SystemExit(main())
