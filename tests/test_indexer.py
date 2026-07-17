"""Tests for token_saver.indexer streaming scan/re-embed (E05) and
incremental fingerprinting (E06)."""
from __future__ import annotations

import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from token_saver.config import init_workspace
from token_saver.indexer import (
    _index_one,
    _savepoint,
    connect,
    index_workspace,
    scan_files,
)
from token_saver.vectors import HashedTFEmbedder


def _multi_chunk_markdown(n_sections: int, words_per_section: int = 500) -> str:
    """A markdown document guaranteed to split into >= n_sections chunks."""
    parts = []
    for i in range(n_sections):
        body = " ".join(f"word{j}" for j in range(words_per_section))
        parts.append(f"## Section {i}\n\n{body}\n")
    return "\n".join(parts)


class _FailOnNthEmbed:
    name = "hashed_tf"

    def __init__(self, fail_at: int):
        self.fail_at = fail_at
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("synthetic embed failure")
        return HashedTFEmbedder().embed(text)


class _FailOnSentinelEmbed:
    name = "hashed_tf"

    def embed(self, text: str):
        if "POISON_SENTINEL" in text:
            # index_workspace()'s per-file catch is deliberately narrow
            # (OSError, sqlite3.Error) -- matching the original code's
            # tolerance scope, not a blanket swallow of arbitrary bugs.
            raise OSError("synthetic embed failure")
        return HashedTFEmbedder().embed(text)


class IndexerStreamingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_files_is_a_generator_not_a_list(self):
        init_workspace(self.tmp)
        (self.tmp / "a.md").write_text("# hi", encoding="utf-8")
        result = scan_files(self.tmp, 20_000_000, False)
        self.assertIsInstance(result, types.GeneratorType)
        # init_workspace() also writes .tokensaverignore, which scan_files
        # legitimately picks up too (only directory names starting with "."
        # are filtered; ignore-file matching is a separate, content-based
        # rule that doesn't exclude the ignore file itself).
        self.assertIn(self.tmp / "a.md", list(result))

    def test_savepoint_rolls_back_partial_file_on_mid_file_failure(self):
        init_workspace(self.tmp)
        path = self.tmp / "multi.md"
        path.write_text(_multi_chunk_markdown(3), encoding="utf-8")
        con = connect(self.tmp)
        embedder = _FailOnNthEmbed(fail_at=2)  # fails embedding the 2nd chunk
        with self.assertRaises(RuntimeError):
            with _savepoint(con, "test_sp"):
                _index_one(con, self.tmp, path, 1600, embedder)
        con.commit()
        files = con.execute(
            "SELECT COUNT(*) FROM files WHERE path='multi.md'").fetchone()[0]
        chunks = con.execute(
            "SELECT COUNT(*) FROM chunks WHERE path='multi.md'").fetchone()[0]
        con.close()
        self.assertEqual(files, 0)  # no partial files row survives the rollback
        self.assertEqual(chunks, 0)  # no orphaned partial chunks survive the rollback
        self.assertEqual(embedder.calls, 2)  # confirms the failure genuinely fired mid-file

    def test_index_workspace_reports_failures_and_still_indexes_other_files(self):
        (self.tmp / "good.md").write_text("# Good\n\nordinary content.\n", encoding="utf-8")
        (self.tmp / "bad.md").write_text("# Bad\n\nPOISON_SENTINEL content.\n", encoding="utf-8")
        init_workspace(self.tmp)
        with mock.patch("token_saver.indexer.get_embedder", return_value=_FailOnSentinelEmbed()):
            stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_failed"], 1)
        con = connect(self.tmp)
        paths = {row[0] for row in con.execute("SELECT path FROM files").fetchall()}
        con.close()
        # good.md (and init_workspace()'s own .tokensaverignore) index fine;
        # bad.md's partial write was rolled back and never committed.
        self.assertIn("good.md", paths)
        self.assertNotIn("bad.md", paths)

    def test_reembed_streams_via_fetchmany_and_updates_every_chunk(self):
        for i in range(5):
            (self.tmp / f"doc{i}.md").write_text(
                _multi_chunk_markdown(3, words_per_section=50), encoding="utf-8")
        init_workspace(self.tmp)
        stats = index_workspace(self.tmp)
        chunk_count = stats["chunks"]
        self.assertGreaterEqual(chunk_count, 15)  # 5 files x 3 sections, sanity floor

        con = connect(self.tmp)
        con.execute("UPDATE meta SET value='fake_old_backend' WHERE key='embedding_backend'")
        sentinel = b"\xff" * (384 * 4)
        con.execute("UPDATE vectors SET vec=?", (sentinel,))
        con.commit()
        con.close()

        stats2 = index_workspace(self.tmp)
        self.assertEqual(stats2["reembedded"], chunk_count)
        self.assertEqual(stats2["files_indexed"], 0)  # no re-scan/re-chunk, re-embed only

        con = connect(self.tmp)
        remaining_sentinels = con.execute(
            "SELECT COUNT(*) FROM vectors WHERE vec=?", (sentinel,)).fetchone()[0]
        con.close()
        self.assertEqual(remaining_sentinels, 0)  # every vector was actually rewritten


if __name__ == "__main__":
    unittest.main()
