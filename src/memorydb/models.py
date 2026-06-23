"""Generic domain model for the MemoryDB substrate (TD-002).

The substrate knows only ``Node`` / ``Edge`` / ``Vector``. Adapters (code, memory) map
their concepts onto these. No code- or memory-specific fields live here.

Models are pydantic ``BaseModel``s — validation + ergonomics over plain dataclasses (TD-004, revised
2026-06-22: pydantic is an allowed core dependency). Construct with keyword arguments.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Intent(str, Enum):
    """Retrieval intents the planner routes on (TD-007)."""

    LOCATE = "LOCATE"   # exact graph lookup ("where is X used?")
    EXPLAIN = "EXPLAIN"  # vector seed -> graph expansion ("how does X work?")
    FILTER = "FILTER"   # SQL over attributes (adapter-specific)


class Rel(str, Enum):
    """Common relation labels. A ``str`` enum so a member is usable directly as the edge ``relation``
    (stored as its value, e.g. ``"CALLS"``). Not exhaustive — adapters may also pass a raw string for
    a relation not listed here (``Edge.relation`` is typed ``str``)."""

    CALLS = "CALLS"
    IMPORTS = "IMPORTS"
    INHERITS = "INHERITS"
    USES = "USES"
    READS = "READS"
    WRITES = "WRITES"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"
    STORES = "STORES"
    PRODUCES = "PRODUCES"


class Node(BaseModel):
    """A generic graph node. ``uid`` is the stable external identity (e.g. a symbol FQN)."""

    uid: str
    type: str
    name: str
    body: Optional[str] = None
    attrs: dict = Field(default_factory=dict)
    source: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)   # bounded so a bogus value can't win MAX (R6-18)

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


class Edge(BaseModel):
    """A directed, typed relation between two nodes (by uid)."""

    src: str
    dst: str
    relation: str
    weight: float = Field(default=1.0, ge=0.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)  # coarse/heuristic edges get < 1.0 (TD-005, R6-18)
    source: Optional[str] = None
