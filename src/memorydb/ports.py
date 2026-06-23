"""Injectable ports (TD-002). The substrate depends on these Protocols, not concretions.

  * ``Embedder``         — supplied by the inference framework (it owns the models)
  * ``IntentClassifier`` — default regex impl in planner.py; swappable for an LLM router
  * ``Extractor``        — implemented by adapters (e.g. the tree-sitter CodeAdapter)
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Map a batch of texts to a batch of vectors (one per text)."""
        ...


@runtime_checkable
class IntentClassifier(Protocol):
    def classify(self, query: str):
        """Return a memorydb.models.Intent for a natural-language query."""
        ...


@runtime_checkable
class Extractor(Protocol):
    def extract(self, path: str, data: bytes = None):
        """Parse a file and return an ``Extraction`` (nodes, edges, pending) to upsert. ``data`` is the
        file's already-read bytes, passed by the indexer to avoid a re-read (optional)."""
        ...
