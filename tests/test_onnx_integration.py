"""Gated integration test for the real ONNX MiniLM embedder.

Skipped by default — downloads a ~23MB model on first run and needs
onnxruntime + tokenizers installed. Opt in with:

    TOKENSAVER_TEST_ONNX=1 python -m unittest tests.test_onnx_integration
"""
from __future__ import annotations

import math
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
