"""Tests for token_saver.indexer streaming scan/re-embed (E05) and
incremental fingerprinting (E06)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from token_saver.config import init_workspace
from token_saver.indexer import (
    _FINGERPRINT_SAMPLE_BYTES,
    UnsafeWorkspacePath,
    _is_link_or_reparse,
    _index_one,
    _sample_fingerprint,
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


class IndexerFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_size_preserved_mtime_replacement_is_detected(self):
        """The exact bug E06 fixes: mtime+size alone can't tell a genuinely
        unchanged file from a same-size replacement whose mtime happens to
        be restored to its old value (e.g. some backup/restore/checkout
        tools). The sampled fingerprint catches what mtime+size miss."""
        init_workspace(self.tmp)
        f = self.tmp / "a.md"
        f.write_text("# Original\n\nAAAA content here.\n", encoding="utf-8")
        index_workspace(self.tmp)
        st1 = f.stat()

        new_text = "# Original\n\nBBBB content here.\n"
        self.assertEqual(len(new_text), len(f.read_text(encoding="utf-8")))
        f.write_text(new_text, encoding="utf-8")
        os.utime(f, (st1.st_atime, st1.st_mtime))
        st2 = f.stat()
        self.assertEqual(st2.st_mtime, st1.st_mtime)
        self.assertEqual(st2.st_size, st1.st_size)

        stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 1)  # must NOT be skipped

        con = connect(self.tmp)
        text = con.execute("SELECT text FROM chunks WHERE path='a.md'").fetchone()[0]
        con.close()
        self.assertIn("BBBB", text)
        self.assertNotIn("AAAA", text)

    def test_unchanged_file_skips_full_sha256(self):
        init_workspace(self.tmp)
        (self.tmp / "a.md").write_text("# stable content\n", encoding="utf-8")
        index_workspace(self.tmp)  # establishes mtime_ns + fingerprint

        def _boom(path):
            raise AssertionError("full SHA-256 hashing should be skipped for unchanged files")

        with mock.patch("token_saver.indexer._sha256", side_effect=_boom):
            stats = index_workspace(self.tmp)
        self.assertEqual(stats["files_indexed"], 0)

    def test_fingerprint_ignores_changes_outside_sampled_windows(self):
        """Proves _sample_fingerprint genuinely only reads head/mid/tail
        windows rather than the whole file: a mutation placed well outside
        all three sampled windows must not change the fingerprint."""
        size = _FINGERPRINT_SAMPLE_BYTES * 10
        base = bytes([65]) * size  # b"A" * size
        path_a = self.tmp / "a.bin"
        path_a.write_bytes(base)
        fp_a = _sample_fingerprint(path_a, size)

        mutated = bytearray(base)
        unsampled_offset = size // 4  # outside head [0,4096), mid, and tail windows
        mutated[unsampled_offset] = 90  # b"Z"
        path_b = self.tmp / "b.bin"
        path_b.write_bytes(bytes(mutated))
        fp_b = _sample_fingerprint(path_b, size)

        self.assertEqual(fp_a, fp_b)

    def test_fingerprint_detects_change_inside_head_window(self):
        size = _FINGERPRINT_SAMPLE_BYTES * 10
        base = bytes([65]) * size
        path_a = self.tmp / "a.bin"
        path_a.write_bytes(base)
        fp_a = _sample_fingerprint(path_a, size)

        mutated = bytearray(base)
        mutated[10] = 90  # inside the head window
        path_b = self.tmp / "b.bin"
        path_b.write_bytes(bytes(mutated))
        fp_b = _sample_fingerprint(path_b, size)

        self.assertNotEqual(fp_a, fp_b)

    def test_schema_migration_adds_columns_to_pre_existing_index(self):
        """A pre-E06 on-disk index (files table without mtime_ns/fingerprint)
        must migrate in place: connect() adds the columns, existing rows get
        safe defaults that force one real verification pass, nothing is lost.
        """
        from token_saver.config import index_path, ts_dir

        ts_dir(self.tmp).mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(index_path(self.tmp))
        raw.executescript("""
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                sha256 TEXT NOT NULL,
                mtime REAL NOT NULL,
                size INTEGER NOT NULL,
                ftype TEXT NOT NULL,
                ntokens INTEGER NOT NULL DEFAULT 0
            );
        """)
        raw.execute(
            "INSERT INTO files(path, sha256, mtime, size, ftype, ntokens) VALUES (?,?,?,?,?,?)",
            ("legacy.md", "deadbeef", 12345.0, 10, "md", 5))
        raw.commit()
        raw.close()

        con = connect(self.tmp)  # must migrate without error
        cols = {row[1] for row in con.execute("PRAGMA table_info(files)").fetchall()}
        self.assertIn("mtime_ns", cols)
        self.assertIn("fingerprint", cols)
        row = con.execute(
            "SELECT path, mtime_ns, fingerprint FROM files WHERE path='legacy.md'").fetchone()
        con.close()
        self.assertEqual(row, ("legacy.md", 0, ""))  # safe migrated defaults

    def test_migrated_row_self_heals_on_next_index_run(self):
        """After migration, the first real index run for a legacy row does
        one genuine verification pass (mtime_ns=0/fingerprint='' can't match
        anything real) and then the fast path works normally afterward.

        Checks chunk-id stability rather than the aggregate files_indexed
        count, since init_workspace() also writes a real .tokensaverignore
        file that legitimately gets indexed alongside legacy.md.
        """
        init_workspace(self.tmp)
        f = self.tmp / "legacy.md"
        f.write_text("# legacy content\n", encoding="utf-8")
        con = connect(self.tmp)
        st = f.stat()
        from token_saver.indexer import _sha256
        fcur = con.execute(
            "INSERT INTO files(path, sha256, mtime, mtime_ns, size, ftype, ntokens, fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("legacy.md", _sha256(f), st.st_mtime, 0, st.st_size, "md", 3, ""),
        )
        con.execute(
            "INSERT INTO chunks(file_id, path, section, heading_path, start_line, end_line, "
            "page, text, ntokens) VALUES (?,?,?,?,?,?,?,?,?)",
            (fcur.lastrowid, "legacy.md", "", "", 1, 1, None, "# legacy content", 3),
        )
        con.commit()
        con.close()

        index_workspace(self.tmp)  # content matches via full-hash fallback
        con = connect(self.tmp)
        chunk_id_1 = con.execute(
            "SELECT id FROM chunks WHERE path='legacy.md'").fetchone()[0]
        con.close()

        def _boom(path):
            raise AssertionError("second run should hit the fast path, not full SHA-256")

        with mock.patch("token_saver.indexer._sha256", side_effect=_boom):
            index_workspace(self.tmp)

        con = connect(self.tmp)
        chunk_id_2 = con.execute(
            "SELECT id FROM chunks WHERE path='legacy.md'").fetchone()[0]
        con.close()
        self.assertEqual(chunk_id_1, chunk_id_2)  # never deleted/recreated


class SymlinkBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.external = Path(tempfile.mkdtemp())
        init_workspace(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.external, ignore_errors=True)

    def _symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_disabled_policy_skips_linked_files_and_directories(self):
        target_file = self.tmp / "target.md"
        target_file.write_text("# target", encoding="utf-8")
        target_dir = self.tmp / "target-dir"
        target_dir.mkdir()
        (target_dir / "inside.md").write_text("# inside", encoding="utf-8")
        self._symlink(self.tmp / "file-link.md", target_file)
        self._symlink(self.tmp / "dir-link", target_dir, directory=True)

        paths = {path.relative_to(self.tmp).as_posix()
                 for path in scan_files(self.tmp, 20_000_000, False)}

        self.assertIn("target.md", paths)
        self.assertIn("target-dir/inside.md", paths)
        self.assertNotIn("file-link.md", paths)
        self.assertFalse(any(path.startswith("dir-link/") for path in paths))

    def test_windows_reparse_attribute_is_treated_as_a_link(self):
        fake_stat = types.SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )
        with mock.patch.object(Path, "lstat", return_value=fake_stat):
            self.assertTrue(_is_link_or_reparse(self.tmp / "junction"))

    def test_enabled_policy_follows_internal_once_and_rejects_external_or_ignored(self):
        target = self.tmp / "z-target.md"
        target.write_text("# internal", encoding="utf-8")
        self._symlink(self.tmp / "a-link.md", target)
        outside = self.external / "outside.md"
        outside.write_text("# private", encoding="utf-8")
        self._symlink(self.tmp / "external.md", outside)
        ignored = self.tmp / ".env"
        ignored.write_text("SECRET=hidden", encoding="utf-8")
        self._symlink(self.tmp / "public-looking.md", ignored)
        unsafe_files: set[str] = set()

        paths = [path.relative_to(self.tmp).as_posix()
                 for path in scan_files(self.tmp, 20_000_000, True,
                                        unsafe_files=unsafe_files)]

        self.assertIn("a-link.md", paths)
        self.assertNotIn("z-target.md", paths)  # same canonical file is indexed once
        self.assertNotIn("external.md", paths)
        self.assertIn("external.md", unsafe_files)
        self.assertNotIn("public-looking.md", paths)  # resolved target is ignored

    def test_enabled_directory_cycle_terminates_without_duplicate_traversal(self):
        real = self.tmp / "z-real"
        real.mkdir()
        (real / "doc.md").write_text("# once", encoding="utf-8")
        self._symlink(self.tmp / "a-link", real, directory=True)
        self._symlink(real / "back", self.tmp, directory=True)

        paths = [path.relative_to(self.tmp).as_posix()
                 for path in scan_files(self.tmp, 20_000_000, True)]

        docs = [path for path in paths if path.endswith("doc.md")]
        self.assertEqual(docs, ["a-link/doc.md"])

    def test_retargeted_link_is_rejected_before_read(self):
        inside = self.tmp / "z-inside.md"
        inside.write_text("# safe", encoding="utf-8")
        link = self.tmp / "a-link.md"
        self._symlink(link, inside)
        outside = self.external / "outside.md"
        outside.write_text("# secret", encoding="utf-8")
        path = next(path for path in scan_files(self.tmp, 20_000_000, True)
                    if path.name == "a-link.md")
        link.unlink()
        self._symlink(link, outside)

        con = connect(self.tmp)
        with self.assertRaises(UnsafeWorkspacePath):
            _index_one(con, self.tmp, path, 1600, HashedTFEmbedder(), True)
        con.close()

    def test_unsafe_retarget_does_not_delete_previously_indexed_row(self):
        path = self.tmp / "document.md"
        path.write_text("# previously safe", encoding="utf-8")
        index_workspace(self.tmp)
        path.unlink()
        outside = self.external / "outside.md"
        outside.write_text("# secret", encoding="utf-8")
        self._symlink(path, outside)
        config_path = self.tmp / ".tokensaver" / "config.json"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                '"follow_symlinks": false', '"follow_symlinks": true'),
            encoding="utf-8",
        )

        index_workspace(self.tmp)

        con = connect(self.tmp)
        count = con.execute(
            "SELECT COUNT(*) FROM files WHERE path='document.md'").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
