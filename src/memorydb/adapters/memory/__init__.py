"""MemoryAdapter — agent memory on the generic substrate (memory-adapter-agent-memory spec, TD-002).

The *second product* on the same `Store`/planner: a long-lived external brain for an agent — entities,
relations, and three memory tiers — proving the substrate generalizes beyond code. Memory concepts map onto
plain `Node`/`Edge`; the substrate core never learns about memory.

Tiers (TD-008) map to a node ``type`` + ``attrs.tier``:
  * **episodic**  → ``Episode``  — a timestamped event/utterance ("Yesterday Guido said X").
  * **semantic**  → ``Fact``     — a deduplicated fact ("Guido created Spruned").
  * **procedural**→ ``Procedure``— a how-to with ordered ``STEP_OF`` steps.

Entities (``Entity`` nodes) link to memories via ``ABOUT`` (facts/procedures) / ``MENTIONS`` (episodes);
arbitrary entity↔entity relations go through :meth:`relate` (open vocabulary, like code's ``Rel``).
Contradictions are **kept**, not overwritten (resolution is the deferred temporal-confidence spec's job,
TD-008/TD-009): two different statements get two ``Fact`` nodes; only *identical* text dedupes.
"""
from __future__ import annotations

import hashlib
import re

from memorydb import query as Q
from memorydb.embedding_pipeline import EmbeddingPipeline
from memorydb.models import Node
from memorydb.vector import BruteForceVectorIndex

from .serializer import MemorySerializer

# tier -> (node type, entity-link relation)
_TIERS = {
    "episodic": ("Episode", "MENTIONS"),
    "semantic": ("Fact", "ABOUT"),
    "procedural": ("Procedure", "ABOUT"),
}
_ENTITY_AUTO_CONFIDENCE = 0.5   # an entity auto-created from a mention is a low-confidence guess


def _norm(text: str) -> str:
    """Normalized identity key: trimmed, lowercased, whitespace-collapsed."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _hash(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def _label(text: str, cap: int = 80) -> str:
    """A short display name (first line, capped) — the body holds the full memory."""
    first = str(text).strip().splitlines()[0] if str(text).strip() else str(text)
    return first[:cap]


def _entity_uid(name: str) -> str:
    return f"entity::{_norm(name)}"


class MemoryAdapter:
    """``remember`` / ``relate`` / ``entity`` / ``recall`` over a shared :class:`Store` + embedder. Brute-
    force vectors by default (memory graphs are small/dense); inject an ``index`` to override."""

    def __init__(self, store, embedder, *, index=None, serializer=None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = index or BruteForceVectorIndex(store)
        self.pipeline = EmbeddingPipeline(store, embedder, serializer=serializer or MemorySerializer())

    # --- writes ------------------------------------------------------------
    def entity(self, name: str, type: str = "Entity", **attrs) -> int:
        """Idempotent upsert of an entity, keyed on the normalized name. An *explicit* entity is
        high-confidence (Node default 1.0) — it upgrades one previously auto-created from a mention."""
        nid = self.store.upsert_node(Node(uid=_entity_uid(name), type=type, name=name,
                                          attrs=attrs, source="memory"))
        self.store.commit()
        return nid

    def remember(self, text: str, *, kind: str = "episodic", entities=(), source: str = "chat",
                 at: "str | None" = None, confidence: float = 1.0, steps=()) -> int:
        """Store a memory in tier ``kind`` (episodic|semantic|procedural), linking each entity (auto-created
        if unknown). Semantic/procedural nodes are identity-keyed by normalized text (re-``remember`` of the
        same fact dedupes and *reinforces* confidence toward 1.0); an episode is keyed by (text, time,
        source) so the same utterance at a different time is a distinct event. ``steps`` (procedural) creates
        ordered ``Step --STEP_OF--> Procedure`` edges. Returns the node id."""
        kind = kind.lower()
        if kind not in _TIERS:
            raise ValueError(f"unknown kind {kind!r} (episodic|semantic|procedural)")
        ntype, rel = _TIERS[kind]
        if kind == "episodic":
            uid = f"episode::{_hash(_norm(text), str(at or ''), str(source))}"
        else:                                   # semantic / procedural -> identity by normalized content
            uid = f"{ntype.lower()}::{_hash(_norm(text))}"

        conf = confidence
        existing = self.store.id_for(uid)
        if existing is not None:                # dedupe: reinforce confidence instead of duplicating
            prev = self.store.get_nodes([existing])[0]["confidence"]
            conf = min(1.0, prev + (1.0 - prev) * 0.5)

        attrs = {"tier": kind}
        if at:
            attrs["at"] = at
        nid = self.store.upsert_node(Node(uid=uid, type=ntype, name=_label(text), body=text,
                                          attrs=attrs, source=source, valid_from=at, confidence=conf))
        for ename in entities:
            self.store.upsert_edge(self._ensure_entity(ename), uid, rel,
                                   confidence=confidence, source=source)
        for i, step in enumerate(steps):
            suid = f"step::{_hash(uid, str(i))}"
            self.store.upsert_node(Node(uid=suid, type="Step", name=_label(step), body=step,
                                        attrs={"tier": "procedural", "order": i}, source=source))
            self.store.upsert_edge(suid, uid, "STEP_OF", weight=float(i), confidence=confidence, source=source)
        self.store.commit()
        return nid

    def relate(self, src: str, relation: str, dst: str, *, confidence: float = 1.0, source=None) -> None:
        """A directed, typed relation between two entities (auto-created if unknown), e.g.
        ``relate("Guido", "WORKS_ON", "Spruned")``."""
        self.store.upsert_edge(self._ensure_entity(src), self._ensure_entity(dst), relation,
                               confidence=confidence, source=source or "memory")
        self.store.commit()

    def _ensure_entity(self, name: str) -> str:
        """Entity uid, creating a low-confidence node only if it does not already exist (never downgrades an
        explicit one). Single idempotent path shared by remember/relate (Review remediation)."""
        uid = _entity_uid(name)
        if self.store.id_for(uid) is None:
            self.store.upsert_node(Node(uid=uid, type="Entity", name=name, source="memory",
                                        confidence=_ENTITY_AUTO_CONFIDENCE))
        return uid

    # --- reads -------------------------------------------------------------
    def recall(self, query: str, *, kinds=("episodic", "semantic", "procedural"), k: int = 8,
               depth: int = 2) -> dict:
        """Retrieve memories relevant to ``query``: (re)embed dirty memory nodes, vector-seed restricted to
        the requested tiers' node types (+ ``Entity`` connectors), then expand over the entity graph. Returns
        ``{query, seeds, nodes, edges}`` (the same shape the planner's EXPLAIN returns)."""
        self.pipeline.refresh()                 # lazy flush: embed anything remembered since last recall
        types = tuple({_TIERS[kind.lower()][0] for kind in kinds if kind.lower() in _TIERS} | {"Entity"})
        qvec = self.embedder.embed([query])[0]
        seeds = [nid for score, nid in self.index.search(qvec, k=k, types=types) if score > 1e-9]
        reached = Q.traverse(self.store, seeds, max_depth=depth, direction="both")
        ids = [r["id"] for r in reached]
        return {"query": query, "seeds": seeds, "nodes": self.store.get_nodes(ids),
                "edges": Q.subgraph_edges(self.store, ids)}

    def steps_of(self, procedure: str) -> list:
        """Ordered step texts of a procedure (by name or uid) — its incoming ``STEP_OF`` edges, ordered by
        the step's ``attrs.order``."""
        puid = procedure if procedure.startswith("procedure::") else f"procedure::{_hash(_norm(procedure))}"
        pid = self.store.id_for(puid)
        if pid is None:
            return []
        rows = self.store.conn.execute(
            "SELECT s.body AS body FROM edges e JOIN nodes s ON s.id = e.src "
            "WHERE e.dst = ? AND e.relation = 'STEP_OF' "
            "ORDER BY CAST(json_extract(s.attrs, '$.order') AS INTEGER)",
            (pid,),
        ).fetchall()
        return [r[0] for r in rows]
