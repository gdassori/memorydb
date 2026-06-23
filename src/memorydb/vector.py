"""Vector storage + similarity (TD-004).

float32 BLOBs in the ``embeddings`` table. The default index is pure-Python exact cosine
(``BruteForceVectorIndex``) — stdlib-only, fine to ~1e5 vectors. ``SqliteVecIndex`` is the
optional ``[vector]`` accelerator behind the same interface (not implemented in v0).
"""
from __future__ import annotations

import array
import heapq
import math
import sqlite3
from typing import Optional, Sequence


def pack(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def normalize(vec: Sequence[float]) -> list:
    """Scale to unit L2 norm (a zero vector stays zero). Embeddings are stored normalized so cosine
    reduces to a dot product at query time (perf MR-4)."""
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class BruteForceVectorIndex:
    """Exact cosine over every stored embedding. O(n) per query (TD-004). Since both the query and the
    stored vectors are unit-normalized, cosine is a plain dot product — no per-vector L2 norm."""

    def __init__(self, store) -> None:
        self.store = store

    def search(
        self,
        query_vec: Sequence[float],
        k: int = 10,
        types: Optional[Sequence[str]] = None,
    ) -> list[tuple[float, int]]:
        q = normalize(list(query_vec))
        dim = len(q)
        # Only score vectors of the query's dimension — a mixed-dim corpus would otherwise truncate via
        # zip() and yield garbage scores (correctness MR-12). Type filter is pushed into SQL too (I7).
        sql = ("SELECT e.node_id AS node_id, e.vector AS vector, n.uid AS uid "
               "FROM embeddings e JOIN nodes n ON n.id = e.node_id WHERE e.dim = ?")
        params: list = [dim]
        if types:
            sql += " AND n.type IN (%s)" % ",".join("?" for _ in types)
            params += list(types)
        rows = self.store.conn.execute(sql, params).fetchall()
        scored = [(sum(a * b for a, b in zip(q, unpack(r["vector"]))), r["node_id"], r["uid"])
                  for r in rows]
        # k largest by score, ties broken by uid asc (churn-invariant determinism, R3L-4). nsmallest on
        # (-score, uid) is that ordering in O(n log k) instead of a full O(n log n) sort (perf MR-22);
        # max(0, k) clamps a negative k to empty (I13).
        top = heapq.nsmallest(max(0, k), scored, key=lambda t: (-t[0], t[2]))
        return [(s, nid) for s, nid, _uid in top]


class SqliteVecIndex:
    """Optional ANN accelerator backed by the sqlite-vec extension (``[vector]`` extra).

    Not implemented in v0 — the BLOB store stays authoritative; this would build a ``vec0``
    virtual table as an index over it (see docs/specs/active/v0-substrate.md open questions).
    """

    def __init__(self, store) -> None:  # pragma: no cover - stub
        raise NotImplementedError(
            "SqliteVecIndex needs the [vector] extra (sqlite-vec). "
            "Use BruteForceVectorIndex until then (TD-004)."
        )


def make_vector_index(store):
    """Best-available ``VectorIndex`` behind one call: the sqlite-vec ANN accelerator when the
    ``[vector]`` extra is present, else the stdlib brute-force index (TD-004). The facade uses
    this so callers get acceleration for free once it lands, without changing their code. When
    ``SqliteVecIndex`` becomes real it will own the dim/``vec0`` setup; until then this degrades
    cleanly to the exact brute-force scan.

    The except covers every way the accelerator can be unavailable (C7): the current stub raises
    ``NotImplementedError``; a real impl can fail because the extension file is missing
    (``sqlite3.OperationalError``) or because this Python's sqlite was built without
    ``enable_load_extension`` (``AttributeError``), or the package isn't installed (``ImportError``)."""
    try:
        return SqliteVecIndex(store)
    except (NotImplementedError, ImportError, AttributeError, sqlite3.OperationalError):
        return BruteForceVectorIndex(store)
