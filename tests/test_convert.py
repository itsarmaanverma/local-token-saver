from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_saver import convert
from token_saver.config import init_workspace
from token_saver.indexer import index_workspace


class PdfCacheIdentityTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.pdf = self.root / "docs" / "contract.pdf"
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b"first-pdf-body")
        self.rel = "docs/contract.pdf"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _metadata(self) -> dict:
        return json.loads(
            convert.converted_metadata_path(self.root, self.rel).read_text(encoding="utf-8"))

    @mock.patch("token_saver.convert.pdf_to_markdown", return_value="# Contract\n\nbody")
    def test_fresh_conversion_writes_identity_and_reuses_valid_pair(self, render):
        out = convert.ensure_converted(self.root, self.pdf, self.rel)
        self.assertIsNotNone(out)
        metadata = self._metadata()
        self.assertEqual(metadata, {
            "source_sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
            "source_size": self.pdf.stat().st_size,
            "converter_version": convert.CONVERTER_VERSION,
            "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        })

        self.assertEqual(convert.ensure_converted(self.root, self.pdf, self.rel), out)
        self.assertEqual(render.call_count, 1)

    @mock.patch("token_saver.convert.pdf_to_markdown", return_value="# Rebuilt")
    def test_same_size_preserved_mtime_source_replacement_invalidates(self, render):
        original_mtime = self.pdf.stat().st_mtime_ns
        convert.ensure_converted(self.root, self.pdf, self.rel)
        self.pdf.write_bytes(b"other-pdf-body")
        os.utime(self.pdf, ns=(original_mtime, original_mtime))

        convert.ensure_converted(self.root, self.pdf, self.rel)
        self.assertEqual(render.call_count, 2)
        self.assertEqual(
            self._metadata()["source_sha256"], hashlib.sha256(self.pdf.read_bytes()).hexdigest())

    @mock.patch("token_saver.convert.pdf_to_markdown", return_value="# Rebuilt")
    def test_missing_malformed_stale_or_corrupt_identity_rebuilds(self, render):
        out = convert.ensure_converted(self.root, self.pdf, self.rel)
        metadata_path = convert.converted_metadata_path(self.root, self.rel)

        metadata_path.unlink()
        convert.ensure_converted(self.root, self.pdf, self.rel)
        metadata_path.write_text("not json", encoding="utf-8")
        convert.ensure_converted(self.root, self.pdf, self.rel)
        metadata = self._metadata()
        metadata["converter_version"] = convert.CONVERTER_VERSION - 1
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        convert.ensure_converted(self.root, self.pdf, self.rel)
        out.write_text("tampered", encoding="utf-8")
        convert.ensure_converted(self.root, self.pdf, self.rel)

        self.assertEqual(render.call_count, 5)

    @mock.patch("token_saver.convert.pdf_to_markdown", return_value="# New mirror")
    def test_sidecar_replace_failure_never_publishes_false_validity(self, render):
        real_replace = os.replace
        calls = 0

        def fail_second_replace(src, dst):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("sidecar replace failed")
            return real_replace(src, dst)

        with mock.patch("token_saver.convert.os.replace", side_effect=fail_second_replace):
            self.assertIsNone(convert.ensure_converted(self.root, self.pdf, self.rel))
        self.assertFalse(convert.converted_metadata_path(self.root, self.rel).exists())

        self.assertIsNotNone(convert.ensure_converted(self.root, self.pdf, self.rel))
        self.assertEqual(render.call_count, 2)

    @mock.patch("token_saver.convert.pdf_to_markdown", return_value="# Mirror")
    def test_prune_removes_deleted_and_renamed_mirror_pairs(self, _render):
        convert.ensure_converted(self.root, self.pdf, self.rel)
        old_mirror = convert.converted_path(self.root, self.rel)
        old_metadata = convert.converted_metadata_path(self.root, self.rel)

        new_rel = "docs/renamed.pdf"
        self.pdf.rename(self.root / new_rel)
        result = convert.convert_workspace_pdfs(self.root, [new_rel])

        self.assertEqual(result["pdfs_pruned"], 1)
        self.assertFalse(old_mirror.exists())
        self.assertFalse(old_metadata.exists())
        self.assertTrue(convert.converted_path(self.root, new_rel).exists())
        self.assertTrue(convert.converted_metadata_path(self.root, new_rel).exists())

    def test_successful_index_prunes_deleted_pdf_artifacts(self):
        init_workspace(self.root)
        stale_rel = "deleted.pdf"
        mirror = convert.converted_path(self.root, stale_rel)
        metadata = convert.converted_metadata_path(self.root, stale_rel)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_text("stale", encoding="utf-8")
        metadata.write_text("{}", encoding="utf-8")

        index_workspace(self.root)

        self.assertFalse(mirror.exists())
        self.assertFalse(metadata.exists())


if __name__ == "__main__":
    unittest.main()
