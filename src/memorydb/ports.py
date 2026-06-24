"""Injectable ports (TD-002). The substrate depends on these Protocols, not concretions.

  * ``Embedder``         — supplied by the inference framework (it owns the models)
  * ``IntentClassifier`` — default regex impl in planner.py; swappable for an LLM router
  * ``LLMClient``        — minimal text-completion port for the LLM intent router (provider injected)
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
class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:
        """Run one completion and return the model's raw text (expected to be JSON for the intent
        router). The provider (Anthropic/Claude, OpenAI, a local model, …) is injected by the caller —
        this substrate never imports one (TD-002). Implementations should keep the call cheap/cached."""
        ...


@runtime_checkable
class Extractor(Protocol):
    def extract(self, path: str, data: bytes = None):
        """Parse a file and return an ``Extraction`` (nodes, edges, pending) to upsert. ``data`` is the
        file's already-read bytes, passed by the indexer to avoid a re-read (optional)."""
        ...
