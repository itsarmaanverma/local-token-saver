"""Unit tests for the pluggable embedder backend (Phases 1-3):
  - vectors.get_embedder / HashedTFEmbedder / to_blob/from_blob/cosine
  - config.load_config embedding-section merging
  - indexer.index_workspace backend-mismatch re-embed-only pass
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import shutil
import sqlite3
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

from token_saver.config import DEFAULT_CONFIG, index_path, init_workspace, load_config
from token_saver.indexer import index_workspace
from token_saver.vectors import (
    DIM,
    Embedder,
    HashedTFEmbedder,
    cosine,
    from_blob,
    get_embedder,
    to_blob,
)


class GetEmbedderTest(unittest.TestCase):
    def test_no_config_returns_hashed_tf(self):
        e = get_embedder(None)
        self.assertIsInstance(e, HashedTFEmbedder)
        self.assertEqual(e.name, "hashed_tf")

    def test_empty_config_returns_hashed_tf(self):
        e = get_embedder({})
        self.assertIsInstance(e, HashedTFEmbedder)
        self.assertEqual(e.name, "hashed_tf")

    def test_onnx_backend_falls_back_when_model_files_absent(self):
        empty_cache = Path(tempfile.mkdtemp())
        try:
            with patch.dict(
                "os.environ",
                {"XDG_CACHE_HOME": str(empty_cache), "LOCALAPPDATA": str(empty_cache)},
            ):
                e = get_embedder({"embedding": {"backend": "onnx_minilm"}})
            self.assertIsInstance(e, HashedTFEmbedder)
            self.assertEqual(e.name, "hashed_tf")
        finally:
            shutil.rmtree(empty_cache, ignore_errors=True)

    def test_unknown_backend_defaults_to_hashed_tf(self):
        e = get_embedder({"embedding": {"backend": "totally_unknown"}})
        self.assertIsInstance(e, HashedTFEmbedder)
        self.assertEqual(e.name, "hashed_tf")


class HashedTFEmbedderTest(unittest.TestCase):
    def test_embed_returns_normalized_384_vector(self):
        vec = HashedTFEmbedder().embed("automatic contract renewal notice period")
        self.assertEqual(len(vec), DIM)
        norm = math.sqrt(sum(v * v for v in vec))
        self.assertAlmostEqual(norm, 1.0, delta=1e-6)

    def test_embed_empty_string_is_zero_vector(self):
        vec = HashedTFEmbedder().embed("")
        self.assertEqual(len(vec), DIM)
        self.assertTrue(all(v == 0.0 for v in vec))

    def test_embed_deterministic(self):
        e = HashedTFEmbedder()
        v1 = e.embed("renewal obligations notice")
        v2 = e.embed("renewal obligations notice")
        self.assertEqual(list(v1), list(v2))


class CustomEmbedderRoundTripTest(unittest.TestCase):
    """Proves downstream code (to_blob/from_blob/cosine) only depends on the
    Embedder interface, not on HashedTFEmbedder internals."""

    class ConstantEmbedder(Embedder):
        name = "constant_mock"

        def embed(self, text: str) -> array:
            val = 1.0 / math.sqrt(DIM)
            return array("f", [val] * DIM)

    def test_round_trip_and_cosine(self):
        e = self.ConstantEmbedder()
        vec = e.embed("anything")
        blob = to_blob(vec)
        restored = from_blob(blob)
        self.assertEqual(len(restored), DIM)
        self.assertAlmostEqual(cosine(restored, restored), 1.0, places=5)
        self.assertAlmostEqual(cosine(vec, restored), 1.0, places=5)


class ConfigEmbeddingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_config_file_defaults_to_hashed_tf(self):
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["embedding"]["backend"], "hashed_tf")

    def test_user_config_overrides_backend_keeps_other_defaults(self):
        init_workspace(self.tmp)
        cfg_path = self.tmp / ".tokensaver" / "config.json"
        cfg_path.write_text(json.dumps({"embedding": {"backend": "onnx_minilm"}}),
                             encoding="utf-8")
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["embedding"]["backend"], "onnx_minilm")
        self.assertEqual(cfg["retrieval"], DEFAULT_CONFIG["retrieval"])
        self.assertEqual(cfg["indexing"], DEFAULT_CONFIG["indexing"])

    def test_invalid_non_dict_embedding_section_keeps_defaults(self):
        init_workspace(self.tmp)
        cfg_path = self.tmp / ".tokensaver" / "config.json"
        cfg_path.write_text(json.dumps({"embedding": "invalid"}), encoding="utf-8")
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["embedding"]["backend"], "hashed_tf")


class ReembedOnBackendMismatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "a.md").write_text(
            "# Renewal Terms\n\nThe agreement renews automatically.\n", encoding="utf-8")
        (self.tmp / "b.md").write_text(
            "# Termination Clause\n\nEither party may terminate for breach.\n",
            encoding="utf-8")
        init_workspace(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _meta_backend(self, con):
        row = con.execute(
            "SELECT value FROM meta WHERE key='embedding_backend'").fetchone()
        return row[0] if row else None

    def test_first_run_no_reembed_and_backend_recorded(self):
        stats = index_workspace(self.tmp)
        self.assertEqual(stats["reembedded"], 0)
        con = sqlite3.connect(index_path(self.tmp))
        try:
            self.assertEqual(self._meta_backend(con), "hashed_tf")
        finally:
            con.close()

    def test_backend_mismatch_triggers_reembed_only(self):
        stats = index_workspace(self.tmp)
        chunk_count = stats["chunks"]
        self.assertGreater(chunk_count, 0)

        con = sqlite3.connect(index_path(self.tmp))
        try:
            con.execute("UPDATE meta SET value='fake_old_backend' WHERE key='embedding_backend'")
            sentinel = b"\xff" * (DIM * 4)
            con.execute("UPDATE vectors SET vec=?", (sentinel,))
            con.commit()
            files_before = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks_before = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            con.close()

        stats2 = index_workspace(self.tmp)
        self.assertEqual(stats2["reembedded"], chunk_count)
        self.assertEqual(stats2["files_indexed"], 0)  # no re-scan/re-chunk of unchanged files

        con = sqlite3.connect(index_path(self.tmp))
        try:
            files_after = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks_after = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            self.assertEqual(files_after, files_before)
            self.assertEqual(chunks_after, chunks_before)
            blobs = [row[0] for row in con.execute("SELECT vec FROM vectors").fetchall()]
            self.assertTrue(all(b != sentinel for b in blobs))
            self.assertEqual(self._meta_backend(con), "hashed_tf")
        finally:
            con.close()


class OnnxFallbackWarningTest(unittest.TestCase):
    """The onnx_minilm -> hashed_tf fallback must not be silent.

    Regression: the audited build fell back correctly but printed nothing, so
    a real semantic-quality drop went unannounced. get_embedder must emit
    exactly one stderr warning per process on fallback.
    """

    def setUp(self):
        import token_saver.vectors as v
        self._v = v
        v._FALLBACK_WARNED = False  # reset the per-process latch for a clean read
        self.addCleanup(setattr, v, "_FALLBACK_WARNED", False)

    def _force_fallback_capture(self):
        """Force the onnx backend to be unavailable (empty model cache) and
        capture stderr. Works whether or not onnxruntime is installed: with the
        deps present the empty cache trips the missing-model path, without them
        the import error trips first; both funnel through the same fallback."""
        empty_cache = Path(tempfile.mkdtemp())
        buf = io.StringIO()
        try:
            with patch.dict(
                "os.environ",
                {"XDG_CACHE_HOME": str(empty_cache), "LOCALAPPDATA": str(empty_cache)},
            ), contextlib.redirect_stderr(buf):
                e = get_embedder({"embedding": {"backend": "onnx_minilm"}})
        finally:
            shutil.rmtree(empty_cache, ignore_errors=True)
        return e, buf.getvalue()

    def test_fallback_emits_one_stderr_warning(self):
        e, err = self._force_fallback_capture()
        self.assertIsInstance(e, HashedTFEmbedder)
        self.assertEqual(e.name, "hashed_tf")
        self.assertIn("token-saver: onnx_minilm unavailable", err)
        self.assertIn("falling back to hashed_tf", err)
        self.assertIn("semantic quality reduced", err)
        self.assertEqual(err.count("token-saver: onnx_minilm unavailable"), 1)

    def test_warning_is_emitted_once_per_process(self):
        _, err1 = self._force_fallback_capture()   # warms the latch
        self.assertIn("token-saver: onnx_minilm unavailable", err1)
        _, err2 = self._force_fallback_capture()   # latch already set -> silent
        self.assertEqual(err2, "")


if __name__ == "__main__":
    unittest.main()
