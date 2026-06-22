"""Graph-aware embedding pipeline (TD-006, graph-aware-embedding-pipeline spec).

Embeds a node's *serialized neighborhood* (its role in the graph), not raw source, and re-embeds
only `embed_dirty` nodes. The serialization is deterministic so embeddings are reproducible.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional, Protocol

from pydantic import BaseModel

from . import query as Q


class NeighborhoodSerializer(Protocol):
    def serialize(self, store, node) -> str: ...


class DefaultSerializer:
    """Language-agnostic node-context serializer. Type-aware: signature/docstring are optional, so it
    works for code symbols and for non-code nodes (e.g. Concept) alike."""

    def __init__(self, cap: int = 25) -> None:
        self.cap = cap  # max neighbors listed per relation (bounds hub fan-out)

    def serialize(self, store, node) -> str:
        # `node` may be a node-id (fetch it) or an already-fetched row dict — the pipeline passes the
        # row it already read via dirty_nodes(), saving one SELECT per node (perf I9).
        if isinstance(node, int):
            rows = store.get_nodes([node])
            if not rows:
                return ""
            node = rows[0]
        node_id = node["id"]
        attrs = node.get("attrs") or {}
        uid = node["uid"]
        path = attrs.get("file_uid") or (uid.split("::")[0] if "::" in uid else None)
        lines = [f"{node['name']}  ({node['type']}" + (f", {path})" if path else ")")]
        if attrs.get("signature"):
            lines.append(f"signature: {attrs['signature']}")
        if attrs.get("docstring"):
            lines.append(f"docstring: {str(attrs['docstring']).splitlines()[0]}")

        nb = Q.node_neighborhood(store, node_id)
        outg: dict = defaultdict(list)
        ing: dict = defaultdict(list)
        for e in nb["out"]:
            outg[e["relation"]].append(e["name"])
        for e in nb["in"]:
            ing[e["relation"]].append(e["name"])
        for rel in sorted(outg):
            names = sorted(set(outg[rel]))[: self.cap]
            lines.append(f"{rel.lower()}: {', '.join(names)}")
        for rel in sorted(ing):
            label = "called_by" if rel == "CALLS" else f"{rel.lower()}_by"
            names = sorted(set(ing[rel]))[: self.cap]
            lines.append(f"{label}: {', '.join(names)}")
        return "\n".join(lines)


class EmbedReport(BaseModel):
    embedded: int = 0
    batches: int = 0
    failed: int = 0


class EmbeddingPipeline:
    def __init__(self, store, embedder, serializer: Optional[NeighborhoodSerializer] = None,
                 batch_size: int = 128, model: Optional[str] = None) -> None:
        self.store = store
        self.embedder = embedder
        self.serializer = serializer or DefaultSerializer()
        self.batch_size = batch_size
        self.model = model

    def refresh(self) -> EmbedReport:
        """(Re)embed every `embed_dirty` node in batches. Idempotent: a no-op when nothing is dirty."""
        dirty = self.store.dirty_nodes()
        rep = EmbedReport()
        for i in range(0, len(dirty), self.batch_size):
            batch = dirty[i : i + self.batch_size]
            rep.batches += 1
            if self._embed_batch(batch) or self._embed_batch(batch):  # one retry
                rep.embedded += len(batch)
            else:
                rep.failed += len(batch)  # leave dirty; the next refresh retries
        self.store.commit()
        return rep

    def reembed_all(self) -> EmbedReport:
        """Mark every node dirty and refresh — e.g. after an embedding-model change."""
        self.store.conn.execute("UPDATE nodes SET embed_dirty = 1")
        return self.refresh()

    def _embed_batch(self, batch) -> bool:
        try:
            texts = [self.serializer.serialize(self.store, n) for n in batch]
            vecs = self.embedder.embed(texts)
            # Count check FIRST: a short return would otherwise let zip() silently drop the trailing
            # nodes — they'd be reported embedded yet stay embed_dirty forever (correctness I2).
            if len(vecs) != len(batch):
                raise ValueError(f"embedder returned {len(vecs)} vectors for {len(batch)} texts")
            if len({len(v) for v in vecs}) > 1:
                raise ValueError("embedder returned inconsistent vector dimensions")
            for n, vec in zip(batch, vecs):
                self.store.set_embedding(n["id"], vec, model=self.model)
            return True
        except Exception:
            return False
