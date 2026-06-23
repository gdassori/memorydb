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

# Each direction is one or more recursive branches that join `edges` directly on an indexed column
# (edges.src / edges.dst) — so SQLite uses idx_edges_src/idx_edges_dst. The earlier "both" form was a
# single join over a `src..UNION ALL..dst` subquery, which SQLite can't push the working-set id into,
# forcing a full edge-table materialization per query (perf R3L-2). Two parallel indexed branches are
# result-identical and index-using.
def _recursive_branches(direction: str, rel_clause: str) -> str:
    out = (f"SELECT e.dst, r.depth + 1 FROM reach r JOIN edges e ON e.src = r.id "
           f"WHERE r.depth < :max_depth{rel_clause}")
    inb = (f"SELECT e.src, r.depth + 1 FROM reach r JOIN edges e ON e.dst = r.id "
           f"WHERE r.depth < :max_depth{rel_clause}")
    if direction == "out":
        return out
    if direction == "in":
        return inb
    if direction == "both":
        return f"{out} UNION {inb}"
    raise ValueError(f"unknown direction: {direction!r}")


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
        rel_clause = " AND e.relation IN (SELECT value FROM json_each(:rels))"
    sql = (
        "WITH RECURSIVE reach(id, depth) AS ( "
        # Only seed from ids that are real nodes, so traverse never reports a non-existent seed as a
        # depth-0 'reached' node (contract fix MR-20).
        "  SELECT value, 0 FROM json_each(:seeds) WHERE value IN (SELECT id FROM nodes) "
        "  UNION "
        f"  {_recursive_branches(direction, rel_clause)} "
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
    """All edges whose both endpoints are within ``node_ids`` (the induced subgraph).

    The id set is materialized into a TEMP table with an integer PK and JOINed, instead of two
    ``json_each`` IN-subqueries — SQLite couldn't index the latter, making this O(|edges| × |ids|) and
    seconds-slow on a hub (reachable from the public explain/ask/context path). The PK join is indexed
    (perf R6-8).
    """
    if not node_ids:
        return []
    conn = store.conn
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _subgraph_ids(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _subgraph_ids")
    conn.executemany("INSERT OR IGNORE INTO _subgraph_ids(id) VALUES(?)", [(int(i),) for i in node_ids])
    sql = (
        "SELECT s.uid AS src, t.uid AS dst, e.relation AS relation, e.confidence AS confidence "
        "FROM edges e "
        "JOIN _subgraph_ids a ON a.id = e.src "
        "JOIN _subgraph_ids b ON b.id = e.dst "
        "JOIN nodes s ON s.id = e.src JOIN nodes t ON t.id = e.dst "
        "ORDER BY e.confidence DESC"
    )
    return [dict(r) for r in conn.execute(sql).fetchall()]
