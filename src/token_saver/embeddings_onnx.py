"""Optional ONNX MiniLM embedder backend — real sentence embeddings.

Opt-in only. Requires `onnxruntime` + `tokenizers` (see the `embeddings`
extra in pyproject.toml) and a one-time model download performed by
`token-saver setup --with-embeddings` (setup_deps.py). This module never
downloads anything itself — if the deps or model files aren't present it
raises EmbedderUnavailable, and callers (vectors.get_embedder) fall back to
the zero-dependency hashed-TF backend.

Model: Xenova/all-MiniLM-L6-v2, onnx/model_quantized.onnx (INT8, ~23MB,
Apache-2.0), 384-dim output — same shape as the hashed-TF backend, so it's
a drop-in replacement in the `vectors` table.
"""
from __future__ import annotations

import os
from array import array
from pathlib import Path

from .vectors import DIM, Embedder

MODEL_DIR_NAME = "minilm-l6-v2"
MODEL_FILE = "model_quantized.onnx"
TOKENIZER_FILE = "tokenizer.json"
MAX_TOKENS = 256

# Pinned HuggingFace source + expected hashes, used by setup_deps.py's downloader for pinned, hash-verified downloads.
HF_REPO = "Xenova/all-MiniLM-L6-v2"
HF_REVISION = "751bff37182d3f1213fa05d7196b954e230abad9"
MODEL_SHA256 = "afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1"
TOKENIZER_SHA256 = "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0"


class EmbedderUnavailable(Exception):
    """Raised when the ONNX backend's deps or model files aren't present."""


def cache_dir() -> Path:
    """Where `setup --with-embeddings` downloads model files, and where
    this module looks for them. Not created here — setup_deps.py owns
    creation/download; this module only reads."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "token-saver" / "models" / MODEL_DIR_NAME


class OnnxMiniLMEmbedder(Embedder):
    """Real sentence embeddings via a quantized ONNX MiniLM model."""

    name = "onnx_minilm"

    def __init__(self) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise EmbedderUnavailable(
                "onnxruntime/tokenizers not installed — run "
                "`token-saver setup --with-embeddings`"
            ) from exc

        d = cache_dir()
        model_path = d / MODEL_FILE
        tok_path = d / TOKENIZER_FILE
        if not model_path.exists() or not tok_path.exists():
            raise EmbedderUnavailable(
                f"embedding model not found in {d} — run "
                "`token-saver setup --with-embeddings`"
            )

        self._np = np
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)

    def embed(self, text: str) -> array:
        np = self._np
        enc = self._tokenizer.encode(text or "")
        ids = enc.ids or [0]
        attn = [1] * len(ids)
        feed = {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([attn], dtype=np.int64),
        }
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros((1, len(ids)), dtype=np.int64)

        outputs = self._session.run(None, feed)
        token_embeddings = outputs[0][0]  # (seq_len, DIM)
        mask = np.array(attn, dtype=np.float32).reshape(-1, 1)
        summed = (token_embeddings * mask).sum(axis=0)
        pooled = summed / max(float(mask.sum()), 1e-9)  # mean pooling

        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm

        vec = array("f", pooled.astype("float32").tolist())
        if len(vec) != DIM:
            raise EmbedderUnavailable(
                f"unexpected embedding dim {len(vec)}, expected {DIM} "
                "(model/export mismatch)"
            )
        return vec
