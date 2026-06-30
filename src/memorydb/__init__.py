"""MemoryDB — an embedded knowledge substrate (relational + graph + vectors) for local LLMs.

See docs/why-these-choices.md for the design and docs/decisions/ for the TDs.
"""
from __future__ import annotations

from . import query
from .api import ExtractorRegistry, MemoryDB
from .context import ContextBuilder, ContextResult, HeuristicCounter
from .embedders import HashingEmbedder
from .embedding_pipeline import DefaultSerializer, EmbeddingPipeline, EmbedReport
from .indexer import IgnoreMatcher, Indexer, IndexReport
from .filters import build_filter_query
from .graph import GraphView
from .models import Edge, Intent, Node, Rel
from .planner import DefaultIntentClassifier, IntentResult, LLMIntentClassifier, RetrievalPlanner
from .ports import LLMClient
from .query_cache import QueryEmbeddingCache
from .ranker import HybridRanker, RankWeights, Scored
from .store import Store
from .vector import BruteForceVectorIndex, SqliteVecIndex, make_vector_index, pack, unpack

__all__ = [
    "MemoryDB",
    "ContextResult",
    "ContextBuilder",
    "HeuristicCounter",
    "ExtractorRegistry",
    "Store",
    "Node",
    "Edge",
    "Intent",
    "Rel",
    "BruteForceVectorIndex",
    "SqliteVecIndex",
    "make_vector_index",
    "pack",
    "unpack",
    "RetrievalPlanner",
    "DefaultIntentClassifier",
    "LLMIntentClassifier",
    "IntentResult",
    "LLMClient",
    "QueryEmbeddingCache",
    "build_filter_query",
    "GraphView",
    "HybridRanker",
    "RankWeights",
    "Scored",
    "HashingEmbedder",
    "EmbeddingPipeline",
    "DefaultSerializer",
    "EmbedReport",
    "Indexer",
    "IndexReport",
    "IgnoreMatcher",
    "query",
]

__version__ = "0.0.1"
