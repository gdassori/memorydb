"""Retrieval primitives (TD-003, TD-007).

Three composable pieces the planner orchestrates:
  * ``vector_search``  — fuzzy entry points (delegates to a VectorIndex)
  * ``traverse``       — multi-hop graph expansion via a recursive CTE
  * ``references_to``  — exact LOCATE: who points at a symbol
plus ``subgraph_edges`` to materialize a readable subgraph.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

# Each direction is expressed as a normalized (a -> b) edge view so the recursive CTE's
# recursive term is always a single simple SELECT (portable across SQLite versions).
_EDGE_VIEW = {
    "out": "SELECT src AS a, dst AS b, relation AS rel FROM edges",
    "in": "SELECT dst AS a, src AS b, relation AS rel FROM edges",
    "both": (
        "SELECT src AS a, dst AS b, relation AS rel FROM edges "
        "UNION ALL SELECT dst AS a, src AS b, relation AS rel FROM edges"
    ),
}


def vector_search(index, query_vec, k: int = 10, types: Optional[Sequence[str]] = None):
    """Thin pass-through to a VectorIndex; kept here so callers import one query module."""
    return index.search(query_vec, k=k, types=types)


def traverse(
    store,
    seed_ids: Sequence[int],
    max_depth: int = 2,
    relations: Optional[Sequence[str]] = None,
    direction: str = "both",
) -> list[dict]:
    """BFS reachability from ``seed_ids`` up to ``max_depth`` hops via a recursive CTE.

    Returns ``[{"id": int, "depth": int}, ...]`` (each node once, at its minimum depth).
    """
    if not seed_ids:
        return []
    params: dict = {
        "seeds": json.dumps([int(s) for s in seed_ids]),
        "max_depth": int(max_depth),
    }
    rel_clause = ""
    if relations:
        params["rels"] = json.dumps(list(relations))
        rel_clause = " AND e.rel IN (SELECT value FROM json_each(:rels))"
    edge_view = _EDGE_VIEW[direction]
    sql = (
        "WITH RECURSIVE reach(id, depth) AS ( "
        "  SELECT value, 0 FROM json_each(:seeds) "
        "  UNION "
        f"  SELECT e.b, r.depth + 1 FROM reach r JOIN ({edge_view}) e ON e.a = r.id "
        f"  WHERE r.depth < :max_depth{rel_clause} "
        ") SELECT id, MIN(depth) AS depth FROM reach GROUP BY id ORDER BY depth, id"
    )
    return [dict(r) for r in store.conn.execute(sql, params).fetchall()]


def references_to(store, name: str) -> list[dict]:
    """Exact LOCATE: every incoming edge to the node(s) matching ``name`` (or uid).

    Higher-confidence (precise) edges sort first; coarse heuristic edges (TD-005) sink.
    """
    sql = (
        "SELECT src.uid AS src_uid, src.name AS src_name, src.type AS src_type, "
        "       e.relation AS relation, e.confidence AS confidence, tgt.uid AS target_uid "
        "FROM nodes tgt "
        "JOIN edges e ON e.dst = tgt.id "
        "JOIN nodes src ON src.id = e.src "
        "WHERE tgt.name = :n OR tgt.uid = :n "
        "ORDER BY e.confidence DESC, src.uid"
    )
    return [dict(r) for r in store.conn.execute(sql, {"n": name}).fetchall()]


def node_neighborhood(store, node_id: int) -> dict:
    """Incoming + outgoing edges of one node, with neighbor uid/name/relation/confidence.

    The unit the graph-aware embedding serializer (TD-006) turns into text, and a building block
    for LOCATE/EXPLAIN. Returns ``{"out": [...], "in": [...]}`` sorted for determinism.
    """
    out = store.conn.execute(
        "SELECT e.relation AS relation, e.confidence AS confidence, n.uid AS uid, n.name AS name "
        "FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.src = :id "
        "ORDER BY e.relation, n.name",
        {"id": int(node_id)},
    ).fetchall()
    inc = store.conn.execute(
        "SELECT e.relation AS relation, e.confidence AS confidence, n.uid AS uid, n.name AS name "
        "FROM edges e JOIN nodes n ON n.id = e.src WHERE e.dst = :id "
        "ORDER BY e.relation, n.name",
        {"id": int(node_id)},
    ).fetchall()
    return {"out": [dict(r) for r in out], "in": [dict(r) for r in inc]}


def subgraph_edges(store, node_ids: Sequence[int]) -> list[dict]:
    """All edges whose both endpoints are within ``node_ids`` (the induced subgraph)."""
    if not node_ids:
        return []
    ids = json.dumps([int(i) for i in node_ids])
    sql = (
        "SELECT s.uid AS src, t.uid AS dst, e.relation AS relation, e.confidence AS confidence "
        "FROM edges e JOIN nodes s ON s.id = e.src JOIN nodes t ON t.id = e.dst "
        "WHERE e.src IN (SELECT value FROM json_each(:ids)) "
        "  AND e.dst IN (SELECT value FROM json_each(:ids)) "
        "ORDER BY e.confidence DESC"
    )
    return [dict(r) for r in store.conn.execute(sql, {"ids": ids}).fetchall()]
