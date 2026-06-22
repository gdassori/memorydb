"""Generic domain model for the MemoryDB substrate (TD-002).

The substrate knows only ``Node`` / ``Edge`` / ``Vector``. Adapters (code, memory) map
their concepts onto these. No code- or memory-specific fields live here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    """Retrieval intents the planner routes on (TD-007)."""

    LOCATE = "LOCATE"   # exact graph lookup ("where is X used?")
    EXPLAIN = "EXPLAIN"  # vector seed -> graph expansion ("how does X work?")
    FILTER = "FILTER"   # SQL over attributes (adapter-specific)


class Rel:
    """Common relation labels. Open-ended — adapters may introduce others."""

    CALLS = "CALLS"
    IMPORTS = "IMPORTS"
    INHERITS = "INHERITS"
    USES = "USES"
    READS = "READS"
    WRITES = "WRITES"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    STORES = "STORES"
    PRODUCES = "PRODUCES"


@dataclass
class Node:
    """A generic graph node. ``uid`` is the stable external identity (e.g. a symbol FQN)."""

    uid: str
    type: str
    name: str
    body: Optional[str] = None
    attrs: dict = field(default_factory=dict)
    source: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    confidence: float = 1.0

    def as_params(self) -> dict:
        return {
            "uid": self.uid,
            "type": self.type,
            "name": self.name,
            "body": self.body,
            "attrs": json.dumps(self.attrs) if self.attrs else None,
            "source": self.source,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "confidence": self.confidence,
        }


@dataclass
class Edge:
    """A directed, typed relation between two nodes (by uid)."""

    src: str
    dst: str
    relation: str
    weight: float = 1.0
    confidence: float = 1.0  # coarse/heuristic edges get < 1.0 (TD-005)
    source: Optional[str] = None
