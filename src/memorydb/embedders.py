"""Dependency-free embedders (TD-004).

``HashingEmbedder`` is a deterministic hashing bag-of-words — it exists so the substrate
runs and tests fully offline. It is NOT semantic-quality; swap in a real model (the
inference framework's ``Embedder``) for production.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Sequence

_TOK = re.compile(r"[a-z0-9_]+")


class HashingEmbedder:
    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _TOK.findall((text or "").lower()):
                bucket = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[bucket] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
