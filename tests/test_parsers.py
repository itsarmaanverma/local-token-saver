"""Tests for token_saver.parsers.parse_csv streaming behavior (E04)."""
from __future__ import annotations

from token_saver.parsers import parse_csv, parse_text


def _old_parse_csv(text: str, rel: str, chunk_chars: int):
    """Reference re-implementation of the OLD list-based parse_csv, used to
    verify the new streaming implementation produces byte-identical output.
    """
    import csv
    import io

    from token_saver.parsers import Chunk

    delim = "\t" if rel.endswith(".tsv") else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except csv.Error:
        return parse_text(text, rel, chunk_chars)
    if not rows:
        return []
    header = rows[0]
    sample = rows[1:21]
    body = (
        f"CSV schema ({len(rows) - 1} data rows): columns = {header}\n"
        "Sample rows:\n" + "\n".join(delim.join(r) for r in sample)
    )
    return [Chunk(rel, body[: chunk_chars * 2], "schema+sample", [], 1, min(len(rows), 21))]


def test_small_csv_matches_old_implementation():
    text = "a,b,c\n1,2,3\n4,5,6\n7,8,9\n"
    rel = "small.csv"
    chunk_chars = 1600

    result = parse_csv(text, rel, chunk_chars)
    expected = _old_parse_csv(text, rel, chunk_chars)

    assert len(result) == 1
    chunk = result[0]
    assert chunk.rel_path == expected[0].rel_path
    assert chunk.text == expected[0].text
    assert chunk.section == expected[0].section
    assert chunk.heading_path == expected[0].heading_path
    assert chunk.start_line == expected[0].start_line
    assert chunk.end_line == expected[0].end_line

    # Hardcoded expectation of exact body content.
    expected_body = (
        "CSV schema (3 data rows): columns = ['a', 'b', 'c']\n"
        "Sample rows:\n1,2,3\n4,5,6\n7,8,9"
    )
    assert chunk.text == expected_body
    assert chunk.start_line == 1
    assert chunk.end_line == 4


def _make_csv(num_data_rows: int) -> str:
    lines = ["col1,col2"]
    for i in range(num_data_rows):
        lines.append(f"{i},{i * 2}")
    return "\n".join(lines) + "\n"


def test_exactly_20_data_rows():
    text = _make_csv(20)
    result = parse_csv(text, "twenty.csv", 1600)
    assert len(result) == 1
    chunk = result[0]
    assert "CSV schema (20 data rows)" in chunk.text
    sample_lines = chunk.text.split("Sample rows:\n", 1)[1].splitlines()
    assert len(sample_lines) == 20
    assert chunk.end_line == min(20 + 1, 21) == 21


def test_25_data_rows_sample_capped_count_true():
    text = _make_csv(25)
    result = parse_csv(text, "twentyfive.csv", 1600)
    assert len(result) == 1
    chunk = result[0]
    assert "CSV schema (25 data rows)" in chunk.text
    sample_lines = chunk.text.split("Sample rows:\n", 1)[1].splitlines()
    assert len(sample_lines) == 20
    assert chunk.end_line == min(25 + 1, 21) == 21


def test_header_only_zero_data_rows():
    text = "col1,col2\n"
    result = parse_csv(text, "headeronly.csv", 1600)
    assert len(result) == 1
    chunk = result[0]
    assert "CSV schema (0 data rows)" in chunk.text
    assert chunk.end_line == min(0 + 1, 21) == 1


def test_fully_empty_string_returns_empty_list():
    text = ""
    result = parse_csv(text, "empty.csv", 1600)
    assert result == []


def test_malformed_csv_falls_back_to_parse_text():
    # Exceeding csv.field_size_limit() deterministically raises csv.Error
    # while iterating, deep into a large file -- unlike NUL bytes or
    # unterminated quotes, which the csv module does not reliably reject.
    import csv as csv_module

    original_limit = csv_module.field_size_limit()
    csv_module.field_size_limit(20)
    try:
        rows = ["a,b,c"]
        for i in range(3000):
            rows.append(f"{i},{i},{i}")
        # A field far larger than the 20-char limit, inserted deep into the file.
        rows.insert(1500, "x" * 50 + ",y,z")
        for i in range(3000):
            rows.append(f"{i},{i},{i}")
        text = "\n".join(rows) + "\n"
        rel = "malformed.csv"
        chunk_chars = 1600

        result = parse_csv(text, rel, chunk_chars)
        expected = parse_text(text, rel, chunk_chars)

        assert len(result) == len(expected)
        for r, e in zip(result, expected):
            assert r.rel_path == e.rel_path
            assert r.text == e.text
            assert r.section == e.section
            assert r.heading_path == e.heading_path
            assert r.start_line == e.start_line
            assert r.end_line == e.end_line
    finally:
        csv_module.field_size_limit(original_limit)


def test_tsv_delimiter_handling():
    text = "a\tb\tc\n1\t2\t3\n4\t5\t6\n"
    rel = "data.tsv"
    chunk_chars = 1600

    result = parse_csv(text, rel, chunk_chars)
    expected = _old_parse_csv(text, rel, chunk_chars)

    assert len(result) == 1
    assert result[0].text == expected[0].text
    assert "a\tb\tc" in result[0].text or "['a', 'b', 'c']" in result[0].text
    assert "1\t2\t3" in result[0].text
    assert result[0].end_line == expected[0].end_line


def test_large_synthetic_csv_functional():
    text = _make_csv(5000)
    result = parse_csv(text, "large.csv", 1600)
    assert len(result) == 1
    chunk = result[0]
    assert "CSV schema (5000 data rows)" in chunk.text
    sample_lines = chunk.text.split("Sample rows:\n", 1)[1].splitlines()
    assert len(sample_lines) == 20
    assert chunk.end_line == 21
