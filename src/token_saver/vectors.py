"""Vectorizer backends — pluggable, so a real embedding model can replace
the zero-dependency default without changing anything downstream.

Two interchangeable `Embedder` implementations produce 384-dim, L2-normalized
vectors stored as float32 blobs in SQLite:
  - HashedTFEmbedder: hashed TF over word unigrams+bigrams. Default. No
    model, no download, no network — deterministic across runs/machines.
  - OnnxMiniLMEmbedder (embeddings_onnx.py): real sentence embeddings via a
    quantized ONNX model. Opt-in, requires `setup --with-embeddings`.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
import sys
from abc import ABC, abstractmethod
from array import array

DIM = 384
_WORD = re.compile(r"[a-z0-9_]{2,}")

_STOP = frozenset(
    "the a an and or of to in for on with is are was were be been this that it as "
    "at by from not no if then else when we you they he she its our your their".split()
)


def _tokens(text: str) -> list[str]:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
    grams = list(words)
    grams.extend(f"{a}_{b}" for a, b in zip(words, words[1:]))
    return grams


def _bucket(token: str) -> tuple[int, int]:
    """Stable (dimension, sign) for a token via blake2b."""
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    val = int.from_bytes(h, "big")
    return val % DIM, 1 if (val >> 63) & 1 else -1


class Embedder(ABC):
    """Common interface every vectorizer backend implements."""

    name: str

    @abstractmethod
    def embed(self, text: str) -> array:
        """Return a DIM-length, L2-normalized float array for `text`."""


class HashedTFEmbedder(Embedder):
    name = "hashed_tf"

    def embed(self, text: str) -> array:
        vec = array("f", [0.0] * DIM)
        toks = _tokens(text)
        if not toks:
            return vec
        for tok in toks:
            dim, sign = _bucket(tok)
            vec[dim] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            for i in range(DIM):
                vec[i] /= norm
        return vec


def embed(text: str) -> array:
    """Back-compat module-level entry point — always the hashed-TF backend."""
    return HashedTFEmbedder().embed(text)


_FALLBACK_WARNED = False


def _warn_onnx_fallback(reason: str) -> None:
    """Emit exactly one stderr warning per process when the onnx_minilm tier
    is requested but unavailable and we silently drop to hashed_tf. A silent
    fallback hides a real semantic-quality regression, so the first occurrence
    is surfaced; later calls in the same process stay quiet to avoid warning on
    every index/search invocation."""
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    print(
        f"token-saver: onnx_minilm unavailable ({reason}); falling back to "
        "hashed_tf (semantic quality reduced)",
        file=sys.stderr,
    )


def get_embedder(config: dict | None = None) -> Embedder:
    """Select an embedder backend from workspace config.

    Falls back to hashed_tf when onnx_minilm is requested but its deps or
    model files aren't present yet (they're pulled by
    `token-saver setup --with-embeddings`, not by this call). The fallback is
    announced once per process via _warn_onnx_fallback so it is never silent.
    """
    backend = (config or {}).get("embedding", {}).get("backend", "hashed_tf")
    if backend == "onnx_minilm":
        from .embeddings_onnx import EmbedderUnavailable, OnnxMiniLMEmbedder
        try:
            return OnnxMiniLMEmbedder()
        except EmbedderUnavailable as exc:
            _warn_onnx_fallback(str(exc))
            return HashedTFEmbedder()
    return HashedTFEmbedder()


def to_blob(vec: array) -> bytes:
    return struct.pack(f"{DIM}f", *vec)


def from_blob(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


def cosine(a: array, b: array) -> float:
    # both are L2-normalized, so dot product == cosine similarity
    return sum(x * y for x, y in zip(a, b))
