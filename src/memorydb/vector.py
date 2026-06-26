"""Vector storage + similarity (TD-004).

float32 BLOBs in the ``embeddings`` table. The default index is pure-Python exact cosine
(``BruteForceVectorIndex``) — stdlib-only, fine to ~1e5 vectors. ``SqliteVecIndex`` is the
optional ``[vector]`` accelerator behind the same interface (not implemented in v0).
"""
from __future__ import annotations

import array
import heapq
import logging
import math
import sqlite3
from typing import Optional, Sequence

_LOG = logging.getLogger(__name__)
_EPS = 1e-6   # snap-to-zero floor for the vec0 float32 cosine round-trip (above the ~1e-7 L2 noise; P5-4)


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
    """Accelerator over the sqlite-vec ``vec0`` virtual table (the ``[vector]`` extra). vec0 KNN in
    sqlite-vec 0.1.x is an **exact** brute-force scan in C (not approximate) — the win is C-vs-Python
    speed at the same recall, so results match :class:`BruteForceVectorIndex` exactly.

    The ``embeddings`` BLOB stays **authoritative**; ``vec_items`` is a derived, rebuildable index kept
    in sync by :meth:`Store.set_embedding` → :meth:`upsert`. Vectors are unit-normalized (like
    :class:`BruteForceVectorIndex`), so vec0's default L2 distance ``d`` maps to cosine ``1 − d²/2`` —
    identical *ranking* and comparable *scores* across backends (spec C6), with no dependency on a
    cosine-metric build of sqlite-vec. The table is created **lazily at the embedder's real dim** on the
    first upsert (migrations run before any embedding exists — spec C3) and the dim is persisted in
    ``meta``. A deleted node's stale ``vec_items`` row is inert (``search`` joins to ``nodes``) and
    reclaimed by :meth:`rebuild_index`, the backstop against drift."""

    _META_DIM = "vec0_dim"

    def __init__(self, store, dim: Optional[int] = None) -> None:
        self.store = store
        self.conn = store.conn
        self._load_extension()                 # raises if unavailable -> make_vector_index falls back
        import sqlite_vec
        self._serialize = sqlite_vec.serialize_float32
        persisted = store.get_meta(self._META_DIM)
        self.dim = dim if dim is not None else (int(persisted) if persisted else None)
        if self.dim:
            self._ensure_table(self.dim)

    def _load_extension(self) -> None:
        import sqlite_vec                       # ImportError if the [vector] extra isn't installed
        self.conn.enable_load_extension(True)   # AttributeError if sqlite lacks extension loading
        try:
            sqlite_vec.load(self.conn)
        finally:
            self.conn.enable_load_extension(False)
        self.conn.execute("SELECT vec_version()").fetchone()   # OperationalError if the load didn't take

    def _ensure_table(self, dim: int) -> None:
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
            "node_id integer primary key, embedding float[%d])" % int(dim)
        )
        self.store.set_meta(self._META_DIM, str(int(dim)))
        self.dim = int(dim)

    def _write(self, node_id: int, v: list) -> None:
        # vec0 has no UPSERT (ON CONFLICT) — delete-then-insert is the idempotent upsert for it.
        self.conn.execute("DELETE FROM vec_items WHERE node_id = ?", (int(node_id),))
        self.conn.execute("INSERT INTO vec_items(node_id, embedding) VALUES(?, ?)",
                          (int(node_id), self._serialize(v)))

    # --- sync (called from Store.set_embedding / node deletion) ------------
    def upsert(self, node_id: int, vector: Sequence[float]) -> None:
        v = normalize(list(vector))
        if self.dim and len(v) != self.dim:
            # A dim/model change is a corpus-wide event — a single wrong-dim row must NOT drop the whole
            # index (re-review P5-3). No-op here; the new dim is adopted by rebuild_index() / a full
            # reembed (MemoryDB.refresh_embeddings(full=True) rebuilds).
            _LOG.debug("vec upsert dim %d != index dim %d (node %s) — skipped; rebuild for a dim change",
                       len(v), self.dim, node_id)
            return
        if not self.dim:
            self._ensure_table(len(v))
        try:
            self._write(node_id, v)
        except sqlite3.OperationalError:
            # The lazily-created table can be gone (e.g. a transaction rollback discarded the CREATE while
            # self.dim stayed cached) — re-ensure and retry once so the index self-heals (re-review P5-2).
            self._ensure_table(len(v))
            self._write(node_id, v)

    def remove(self, node_id: int) -> None:
        # Must be called when a node is deleted (Store.index_remove): a stale vec_items row otherwise
        # starves k-NN and, on node-id reuse, scores a NEW node by the deleted node's vector (P5-1).
        try:
            self.conn.execute("DELETE FROM vec_items WHERE node_id = ?", (int(node_id),))
        except sqlite3.OperationalError:
            pass                               # no table yet -> nothing to remove

    # --- query ------------------------------------------------------------
    def search(self, query_vec: Sequence[float], k: int = 10,
               types: Optional[Sequence[str]] = None) -> list[tuple[float, int]]:
        k = max(0, k)                          # negative k -> empty (mirrors BruteForceVectorIndex, I13)
        if not self.dim or k == 0:
            return []
        q = normalize(list(query_vec))
        if len(q) != self.dim:                 # query-dim guard, mirrors the brute-force MR-12 guard
            return []
        qb = self._serialize(q)
        tset = set(types) if types else None
        # ALWAYS over-fetch (not just for a types filter): the nodes-join drops any stale row of a
        # deleted node, so fetching k would underfill k after the drop (starvation) and miss k-boundary
        # ties before the uid tie-break (determinism). Escalate further only when a types filter came up
        # short while the KNN was not yet exhausted (re-review P5-1 / P5-5).
        over = k * 4
        while True:
            try:
                rows = self.conn.execute(
                    "SELECT v.node_id AS node_id, v.distance AS distance, n.uid AS uid, n.type AS type "
                    "FROM vec_items v JOIN nodes n ON n.id = v.node_id "
                    "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                    (qb, over),
                ).fetchall()
            except sqlite3.OperationalError:   # table missing after a rollback -> empty, not a crash (P5-2)
                return []
            kept = rows if tset is None else [r for r in rows if r["type"] in tset]
            if tset is None or len(kept) >= k or len(rows) < over:   # enough, or the KNN is exhausted
                rows = kept
                break
            over *= 4
        scored = []
        for r in rows:
            d = r["distance"]
            s = 1.0 - (d * d) / 2.0
            if -_EPS < s < _EPS:               # snap float32 L2 noise to exact 0 so orthogonal seeds drop
                s = 0.0                         # (matches brute-force cosine 0.0 vs the planner >1e-9 filter, P5-4)
            scored.append((s, r["node_id"], r["uid"]))
        scored.sort(key=lambda t: (-t[0], t[2]))   # score desc, uid asc (matches brute force)
        return [(s, nid) for s, nid, _uid in scored[:k]]

    def rebuild_index(self) -> int:
        """Truncate and repopulate ``vec_items`` from the authoritative ``embeddings`` BLOBs — the
        backstop against drift (deletes, crashes, a dim/model change). Builds at the **prevailing** dim
        (most rows); off-dim rows are skipped with a warning rather than silently dropped. Returns the
        indexed row count."""
        row = self.conn.execute(
            "SELECT dim FROM embeddings GROUP BY dim ORDER BY count(*) DESC, dim LIMIT 1"
        ).fetchone()
        self.conn.execute("DROP TABLE IF EXISTS vec_items")
        self.dim = None
        self.store.set_meta(self._META_DIM, "")
        if row is None:
            return 0
        dim = row[0]
        self._ensure_table(dim)
        n, skipped = 0, 0
        for r in self.conn.execute("SELECT node_id, dim, vector FROM embeddings ORDER BY node_id"):
            if r["dim"] != dim:
                skipped += 1
                continue
            self.conn.execute("INSERT INTO vec_items(node_id, embedding) VALUES(?, ?)",
                              (r["node_id"], self._serialize(list(unpack(r["vector"])))))
            n += 1
        if skipped:
            _LOG.warning("rebuild_index: skipped %d off-dim embedding(s) (prevailing dim %d)", skipped, dim)
        return n


def make_vector_index(store, prefer_ann: bool = True):
    """Best-available ``VectorIndex`` behind one call: the sqlite-vec ANN accelerator when the
    ``[vector]`` extra loads, else the stdlib brute-force index (TD-004). The facade uses this so
    callers get acceleration for free, with the same ``search(query_vec, k, types)`` contract.

    The except covers every way the accelerator can be unavailable (spec C7): the package isn't
    installed (``ImportError``), the extension file won't load (``sqlite3.OperationalError``), or this
    Python's sqlite was built without / has disabled ``enable_load_extension`` (``AttributeError``)."""
    if not prefer_ann:
        return BruteForceVectorIndex(store)
    try:
        return SqliteVecIndex(store)
    except (ImportError, AttributeError, sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        _LOG.debug("sqlite-vec unavailable (%s); using BruteForceVectorIndex", exc)
        return BruteForceVectorIndex(store)
