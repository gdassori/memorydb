"""Vector storage + similarity (TD-004).

float32 BLOBs in the ``embeddings`` table. The default index is pure-Python exact cosine
(``BruteForceVectorIndex``) — stdlib-only, fine to ~1e5 vectors. ``SqliteVecIndex`` is the
optional ``[vector]`` accelerator behind the same interface (not implemented in v0).
"""
from __future__ import annotations

import array
import math
from typing import Optional, Sequence


def pack(vec: Sequence[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def _cosine(q: Sequence[float], q_norm: float, v: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(q, v))
    v_norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return dot / (q_norm * v_norm)


class BruteForceVectorIndex:
    """Exact cosine over every stored embedding. O(n) per query (TD-004)."""

    def __init__(self, store) -> None:
        self.store = store

    def search(
        self,
        query_vec: Sequence[float],
        k: int = 10,
        types: Optional[Sequence[str]] = None,
    ) -> list[tuple[float, int]]:
        q = list(query_vec)
        q_norm = math.sqrt(sum(x * x for x in q)) or 1.0
        rows = self.store.conn.execute(
            "SELECT e.node_id AS node_id, e.vector AS vector, n.type AS type "
            "FROM embeddings e JOIN nodes n ON n.id = e.node_id"
        ).fetchall()
        type_set = set(types) if types else None
        scored: list[tuple[float, int]] = []
        for r in rows:
            if type_set is not None and r["type"] not in type_set:
                continue
            scored.append((_cosine(q, q_norm, unpack(r["vector"])), r["node_id"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[:k]


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
    cleanly to the exact brute-force scan."""
    try:
        return SqliteVecIndex(store)
    except NotImplementedError:
        return BruteForceVectorIndex(store)
