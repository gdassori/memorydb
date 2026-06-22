"""MemoryDB — an embedded knowledge substrate (relational + graph + vectors) for local LLMs.

See docs/why-these-choices.md for the design and docs/decisions/ for the TDs.
"""
from __future__ import annotations

from . import query
from .embedders import HashingEmbedder
from .embedding_pipeline import DefaultSerializer, EmbeddingPipeline, EmbedReport
from .models import Edge, Intent, Node, Rel
from .planner import DefaultIntentClassifier, RetrievalPlanner
from .store import Store
from .vector import BruteForceVectorIndex, SqliteVecIndex, pack, unpack

__all__ = [
    "Store",
    "Node",
    "Edge",
    "Intent",
    "Rel",
    "BruteForceVectorIndex",
    "SqliteVecIndex",
    "pack",
    "unpack",
    "RetrievalPlanner",
    "DefaultIntentClassifier",
    "HashingEmbedder",
    "EmbeddingPipeline",
    "DefaultSerializer",
    "EmbedReport",
    "query",
]

__version__ = "0.0.1"
