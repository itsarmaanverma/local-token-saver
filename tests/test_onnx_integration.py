"""Gated integration test for the real ONNX MiniLM embedder.

Skipped by default — downloads a ~23MB model on first run and needs
onnxruntime + tokenizers installed. Opt in with:

    TOKENSAVER_TEST_ONNX=1 python -m unittest tests.test_onnx_integration
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from token_saver.setup_deps import setup_embeddings
from token_saver.vectors import DIM, cosine


@unittest.skipUnless(
    os.environ.get("TOKENSAVER_TEST_ONNX") == "1",
    "set TOKENSAVER_TEST_ONNX=1 to run the ONNX integration test (downloads ~23MB model)",
)
class OnnxEmbedderIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(
                "onnxruntime/tokenizers not installed — run `pip install .[embeddings]`"
            ) from exc

        ok, report = setup_embeddings(auto_install=False, download_model=True)
        if not ok:
            raise AssertionError(f"setup_embeddings failed:\n{report}")

        from token_saver.embeddings_onnx import OnnxMiniLMEmbedder

        cls.embedder = OnnxMiniLMEmbedder()

    def test_embed_shape_and_norm(self):
        vec = self.embedder.embed("hello world")
        self.assertEqual(len(vec), DIM)
        norm = math.sqrt(sum(v * v for v in vec))
        self.assertAlmostEqual(norm, 1.0, delta=1e-4)

    def test_semantic_ordering(self):
        base = self.embedder.embed("install dependencies with pip")
        similar = self.embedder.embed("use pip to add the required packages")
        unrelated = self.embedder.embed("my cat sleeps on the windowsill all afternoon")

        sim_score = cosine(base, similar)
        unrelated_score = cosine(base, unrelated)

        self.assertGreater(sim_score, unrelated_score)
        self.assertGreater(sim_score - unrelated_score, 0.01)

    def test_deterministic(self):
        v1 = self.embedder.embed("the quick brown fox jumps over the lazy dog")
        v2 = self.embedder.embed("the quick brown fox jumps over the lazy dog")
        self.assertEqual(list(v1), list(v2))

    def test_empty_and_long_input(self):
        empty_vec = self.embedder.embed("")
        self.assertEqual(len(empty_vec), DIM)

        long_text = " ".join(["word{}".format(i) for i in range(2000)])
        long_vec = self.embedder.embed(long_text)
        self.assertEqual(len(long_vec), DIM)
        norm = math.sqrt(sum(v * v for v in long_vec))
        self.assertAlmostEqual(norm, 1.0, delta=1e-4)


@unittest.skipUnless(
    os.environ.get("TOKENSAVER_TEST_ONNX") == "1",
    "set TOKENSAVER_TEST_ONNX=1 to run the ONNX gate regression test (needs the real model)",
)
class OnnxGateRegressionTest(unittest.TestCase):
    """Regression for the recalibrated onnx_minilm pure-vector gate.

    The old 0.94 gate excluded every related pair, so a lexically-disjoint but
    semantically-related query fell through to the pure-vector path and came
    back EMPTY. With the gate at 0.70 the same query must return non-empty
    results. Fixture: one doc plus a query that shares zero index tokens with
    it (guaranteeing the pure-vector fallback); measured cosine ~0.81, which
    passes 0.70 but not the old 0.94.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(
                "onnxruntime/tokenizers not installed — run `pip install .[embeddings]`"
            ) from exc
        ok, report = setup_embeddings(auto_install=False, download_model=True)
        if not ok:
            raise AssertionError(f"setup_embeddings failed:\n{report}")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_related_disjoint_query_returns_nonempty_vector_results(self):
        from token_saver.config import config_path, index_path, init_workspace
        from token_saver.indexer import index_workspace
        from token_saver.retrieval import search

        # Doc vocabulary: acquiring/motorized/vehicle/financing/dealerships.
        # Query vocabulary: car/cash/get/people/hand -> zero shared FTS tokens,
        # so BM25 matches nothing and the pure-vector gate alone decides.
        (self.tmp / "financing.txt").write_text(
            "Acquiring a motorized vehicle for personal transportation requires "
            "selecting appropriate financing options and arranging purchase "
            "agreements with dealerships.\n",
            encoding="utf-8",
        )
        init_workspace(self.tmp)
        config_path(self.tmp).write_text(
            json.dumps({"embedding": {"backend": "onnx_minilm"}}), encoding="utf-8"
        )
        index_workspace(self.tmp)

        # Confirm the fixture actually indexed with onnx (not a silent fallback),
        # otherwise we would be exercising the hashed_tf gate instead.
        con = sqlite3.connect(index_path(self.tmp))
        try:
            backend = con.execute(
                "SELECT value FROM meta WHERE key='embedding_backend'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(backend, "onnx_minilm", "fixture did not index with onnx backend")

        query = "How do people get a car when they don't have enough cash on hand?"
        hits = search(self.tmp, query, top_k=5)
        self.assertTrue(
            hits,
            "pure-vector fallback returned an empty set — the 0.94 empty-set "
            "gate bug has regressed",
        )
        self.assertEqual(hits[0].path, "financing.txt")


if __name__ == "__main__":
    unittest.main()
