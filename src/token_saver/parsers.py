"""File-type-aware parsing into structure-aware chunks.

Each parser yields Chunk objects. Token counts are estimated as len(text)//4.
PDF support requires the optional `pypdf` dependency (pip install local-token-saver[pdf]).
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".r", ".m", ".lua", ".pl",
}
MARKDOWN_EXTS = {".md", ".mdx", ".markdown"}
TEXT_EXTS = {".txt", ".rst", ".text", ".log", ".cfg", ".ini", ".env.example"}
DATA_EXTS = {".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".htm"}
CSV_EXTS = {".csv", ".tsv"}

# a chunk targets ~400 estimated tokens => ~1600 chars
DEFAULT_CHUNK_CHARS = 1600


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    rel_path: str
    text: str
    section: str = ""
    heading_path: list[str] = field(default_factory=list)
    start_line: int = 1
    end_line: int = 1
    page: int | None = None

    @property
    def ntokens(self) -> int:
        return est_tokens(self.text)


def file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MARKDOWN_EXTS:
        return "markdown"
    if ext in CODE_EXTS:
        return "code"
    if ext in CSV_EXTS:
        return "csv"
    if ext in DATA_EXTS:
        return "data"
    if ext == ".pdf":
        return "pdf"
    if ext == ".ipynb":
        return "notebook"
    return "text"


def _split_windows(lines: list[str], rel: str, section: str, heads: list[str],
                   first_line: int, chunk_chars: int) -> list[Chunk]:
    """Greedy line-window split at ~chunk_chars per chunk."""
    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0
    start = first_line
    for i, line in enumerate(lines):
        buf.append(line)
        size += len(line) + 1
        if size >= chunk_chars:
            chunks.append(Chunk(rel, "\n".join(buf), section, list(heads),
                                start, first_line + i))
            buf, size, start = [], 0, first_line + i + 1
    if buf and any(l.strip() for l in buf):
        chunks.append(Chunk(rel, "\n".join(buf), section, list(heads),
                            start, first_line + len(lines) - 1))
    return chunks


def parse_markdown(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    """Heading-aware: split at headings, then window oversized sections."""
    lines = text.splitlines()
    chunks: list[Chunk] = []
    heads: list[str] = []
    sec_lines: list[str] = []
    sec_start = 1

    def flush(end_line: int):
        if sec_lines and any(l.strip() for l in sec_lines):
            section = heads[-1] if heads else ""
            chunks.extend(_split_windows(sec_lines, rel, section, heads,
                                         sec_start, chunk_chars))

    for i, line in enumerate(lines, 1):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush(i - 1)
            level = len(m.group(1))
            heads = heads[: level - 1] + [m.group(2).strip()]
            sec_lines = [line]
            sec_start = i
        else:
            sec_lines.append(line)
    flush(len(lines))
    return chunks


def parse_code(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    """Symbol-boundary-aware: prefer breaks at top-level def/class/function lines."""
    lines = text.splitlines()
    boundary = re.compile(
        r"^(def |class |async def |func |fn |function |public |private |protected "
        r"|impl |export |const [A-Z_]|@)"
    )
    chunks: list[Chunk] = []
    buf: list[str] = []
    size = 0
    start = 1
    current_symbol = ""
    for i, line in enumerate(lines, 1):
        if size >= chunk_chars and boundary.match(line):
            chunks.append(Chunk(rel, "\n".join(buf), current_symbol, [], start, i - 1))
            buf, size, start = [], 0, i
        if boundary.match(line):
            current_symbol = line.strip()[:80]
        buf.append(line)
        size += len(line) + 1
        if size >= chunk_chars * 2:  # hard cap even without a boundary
            chunks.append(Chunk(rel, "\n".join(buf), current_symbol, [], start, i))
            buf, size, start = [], 0, i + 1
    if buf and any(l.strip() for l in buf):
        chunks.append(Chunk(rel, "\n".join(buf), current_symbol, [], start, len(lines)))
    return chunks


def parse_text(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    return _split_windows(text.splitlines(), rel, "", [], 1, chunk_chars)


def parse_csv(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    """Schema summary + sampled rows, not the whole table.

    Streams the CSV one row at a time so memory stays proportional to the
    sample size (~20 rows), instead of materializing the entire table just
    to compute a 20-row sample and a count.
    """
    delim = "\t" if rel.endswith(".tsv") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader, None)
        if header is None:
            return []
        sample: list[list[str]] = []
        total_data_rows = 0
        for row in reader:
            total_data_rows += 1
            if len(sample) < 20:
                sample.append(row)
    except csv.Error:
        return parse_text(text, rel, chunk_chars)
    body = (
        f"CSV schema ({total_data_rows} data rows): columns = {header}\n"
        "Sample rows:\n"
        + "\n".join(delim.join(r) for r in sample)
    )
    return [Chunk(rel, body[: chunk_chars * 2], "schema+sample", [], 1, min(total_data_rows + 1, 21))]


def parse_data(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    """JSON gets a key-path summary chunk; YAML/TOML/XML/HTML chunk as text."""
    if rel.endswith(".json"):
        try:
            obj = json.loads(text)
            paths: list[str] = []

            def walk(o, prefix, depth):
                if depth > 3 or len(paths) > 200:
                    return
                if isinstance(o, dict):
                    for k, v in o.items():
                        paths.append(f"{prefix}.{k}" if prefix else k)
                        walk(v, f"{prefix}.{k}" if prefix else k, depth + 1)
                elif isinstance(o, list) and o:
                    walk(o[0], prefix + "[]", depth + 1)

            walk(obj, "", 0)
            summary = f"JSON key paths: {', '.join(paths[:200])}"
            chunks = [Chunk(rel, summary, "key-paths", [], 1, 1)]
            if len(text) <= chunk_chars * 2:
                chunks.append(Chunk(rel, text, "content", [], 1, text.count("\n") + 1))
            return chunks
        except (json.JSONDecodeError, RecursionError):
            pass
    return parse_text(text, rel, chunk_chars)


def parse_pdf(path: Path, rel: str, chunk_chars: int) -> list[Chunk]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return [Chunk(rel, f"[PDF not indexed: install local-token-saver[pdf] to enable] {path.name}",
                      "unindexed", [], 1, 1)]
    chunks: list[Chunk] = []
    try:
        reader = PdfReader(str(path))
        for pageno, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            for c in _split_windows(text.splitlines(), rel, f"page {pageno}", [], 1, chunk_chars):
                c.page = pageno
                chunks.append(c)
    except Exception as e:  # noqa: BLE001 — malformed PDFs must not kill indexing
        chunks.append(Chunk(rel, f"[PDF extraction failed: {e}]", "error", [], 1, 1))
    return chunks


def parse_file(path: Path, rel: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[Chunk]:
    ftype = file_type(path)
    if ftype == "pdf":
        return parse_pdf(path, rel, chunk_chars)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "\x00" in text[:8192]:  # binary sniff
        return []
    if ftype == "markdown":
        return parse_markdown(text, rel, chunk_chars)
    if ftype == "code":
        return parse_code(text, rel, chunk_chars)
    if ftype == "csv":
        return parse_csv(text, rel, chunk_chars)
    if ftype == "data":
        return parse_data(text, rel, chunk_chars)
    if ftype == "notebook":
        return _parse_notebook(text, rel, chunk_chars)
    return parse_text(text, rel, chunk_chars)


def _parse_notebook(text: str, rel: str, chunk_chars: int) -> list[Chunk]:
    try:
        nb = json.loads(text)
        cells = nb.get("cells", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    chunks: list[Chunk] = []
    for i, cell in enumerate(cells):
        src = "".join(cell.get("source", []))
        if src.strip():
            chunks.append(Chunk(rel, src[: chunk_chars * 2],
                                f"cell {i} ({cell.get('cell_type', '?')})", [], 1, 1))
    return chunks
