"""Neighborhood serializer for agent-memory nodes (memory-adapter-agent-memory spec, TD-006).

Unlike the code ``DefaultSerializer`` — which embeds a symbol's graph *role* (its callers/callees), not
its source — memory recall is about the **content**: a fact's text leads, then its linked entities and the
event time. Deterministic so embeddings are reproducible.
"""
from __future__ import annotations

from memorydb import query as Q


class MemorySerializer:
    """Serialize a memory node (Episode/Fact/Procedure/Entity) for embedding: content text + linked
    entity names + time/source. ``node`` is a node-id or an already-fetched row dict (the pipeline passes
    the row it already read — perf I9)."""

    def __init__(self, cap: int = 25) -> None:
        self.cap = cap   # max linked entities listed (bounds fan-out)

    def serialize(self, store, node) -> str:
        if isinstance(node, int):
            rows = store.get_nodes([node])
            if not rows:
                return ""
            node = rows[0]
        attrs = node.get("attrs") or {}
        # Content leads (the memory itself); an Entity has no body, so fall back to its name.
        lines = [str(node.get("body") or node["name"])]
        nb = Q.node_neighborhood(store, node["id"])
        entities = sorted({e["name"] for e in nb["out"]} | {e["name"] for e in nb["in"]})[: self.cap]
        if entities:
            lines.append("entities: " + ", ".join(entities))
        when = node.get("valid_from") or attrs.get("at")
        if when:
            lines.append(f"when: {when}")
        if node.get("source"):
            lines.append(f"source: {node['source']}")
        return "\n".join(lines)
