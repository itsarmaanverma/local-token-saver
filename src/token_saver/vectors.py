"""Script-based local vectorizer — no LLM, no model downloads, no network.

Hashed TF vectors over word unigrams + bigrams, L2-normalized, stored as
float32 blobs in SQLite. Brute-force cosine works fine at workspace scale
(tens of thousands of chunks). Deterministic across runs and machines.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
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


def embed(text: str) -> array:
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


def to_blob(vec: array) -> bytes:
    return struct.pack(f"{DIM}f", *vec)


def from_blob(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


def cosine(a: array, b: array) -> float:
    # both are L2-normalized, so dot product == cosine similarity
    return sum(x * y for x, y in zip(a, b))
