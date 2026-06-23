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

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _subtokens(text: str) -> list:
    """Each identifier plus its snake_case / CamelCase sub-parts — so `send_notification` and
    `MassNotificationJob` both share the `notification` feature with a query mentioning notifications,
    instead of being one opaque token (R6-22)."""
    toks: list = []
    for ident in _IDENT.findall(text or ""):
        whole = ident.lower()
        toks.append(whole)
        parts = [p for snake in ident.split("_") for p in _CAMEL.findall(snake)]
        for p in parts:
            pl = p.lower()
            if pl and pl != whole:
                toks.append(pl)
    return toks


class HashingEmbedder:
    """Deterministic hashing bag-of-(sub)words. NOT semantic-quality (swap a real model in for
    production), but the sub-token split gives it enough signal for offline tests/EXPLAIN."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _subtokens(text):
                bucket = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[bucket] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
