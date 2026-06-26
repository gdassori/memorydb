"""The Store: a SQLite connection + schema + write/read API (TD-003).

Single source of truth for nodes, edges and embeddings. Maintains the ``embed_dirty``
staleness flag for graph-aware embeddings (TD-006): upserting an edge marks both
endpoints dirty, since their serialized neighborhoods changed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional, Sequence

from .migrations import migrate
from .models import Node
from .vector import normalize, pack


class Store:
    # MemoryDB is single-writer (an embedded, single-process substrate). A second concurrent writer is
    # made to WAIT up to busy_timeout rather than crash immediately (R6-10/R6-11); long index() runs
    # hold the write lock for their whole transaction, so raise this if you genuinely contend.
    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 5000) -> None:
        # timeout= sets the C-level busy handler so a locked DB blocks (then raises) instead of an
        # instant 'database is locked'.
        self.conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0)
        try:
            self.conn.row_factory = sqlite3.Row
            # Connection pragmas live here (not in schema.sql) so the schema stays pure DDL.
            self.conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self.conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":                     # WAL is meaningless for an in-memory DB
                self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")  # safe under WAL, avoids a full fsync/commit
            migrate(self.conn)  # apply pending schema migrations (TD-003 / schema-migrations spec)
        except Exception:
            self.conn.close()                          # don't leak the connection on a failed open (R6-10)
            raise
        self._index = None   # optional VectorIndex notified by set_embedding (sqlite-vec-acceleration)

    def attach_index(self, index) -> None:
        """Register the active ``VectorIndex`` so ``set_embedding`` keeps a derived ANN index (vec0) in
        sync incrementally. A brute-force index (no ``upsert``) is simply ignored — it reads the
        authoritative ``embeddings`` BLOBs directly at query time."""
        self._index = index

    def index_remove(self, node_ids: Sequence[int]) -> None:
        """Notify the active index that nodes are gone (mirrors the ``set_embedding`` upsert hook). A
        derived ANN index (vec0) MUST drop their rows: a stale row starves k-NN and, on SQLite node-id
        reuse, scores a re-indexed node by the deleted node's vector (re-review P5-1). A brute-force
        index (no ``remove``) is ignored. Failures are logged, never raised (the BLOB store is
        authoritative; ``rebuild_index`` is the backstop)."""
        idx = self._index
        if idx is None or not hasattr(idx, "remove"):
            return
        for nid in node_ids:
            try:
                idx.remove(nid)
            except Exception:   # noqa: BLE001 - a derived-index hiccup must not break node deletion
                logging.getLogger(__name__).debug("vec index remove failed for node %s", nid, exc_info=True)

    # --- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- ids ---------------------------------------------------------------
    def id_for(self, uid: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM nodes WHERE uid = ?", (uid,)).fetchone()
        return row[0] if row else None

    # --- meta (key/value substrate metadata; migration 2) ------------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # --- writes ------------------------------------------------------------
    def upsert_node(self, node: Node) -> int:
        row = self.conn.execute(
            "INSERT INTO nodes(uid, type, name, body, attrs, source, valid_from, valid_to, confidence, embed_dirty) "
            "VALUES(:uid, :type, :name, :body, :attrs, :source, :valid_from, :valid_to, :confidence, 1) "
            "ON CONFLICT(uid) DO UPDATE SET "
            "  type=excluded.type, name=excluded.name, body=excluded.body, attrs=excluded.attrs, "
            "  source=excluded.source, valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
            "  confidence=excluded.confidence, embed_dirty=1 "
            "RETURNING id",
            node.as_params(),
        ).fetchone()
        return row[0]

    def upsert_edge(
        self,
        src_uid: str,
        dst_uid: str,
        relation: str,
        weight: float = 1.0,
        confidence: float = 1.0,
        source: Optional[str] = None,
    ) -> None:
        s = self.id_for(src_uid)
        d = self.id_for(dst_uid)
        if s is None or d is None:
            raise KeyError(f"edge endpoints must both exist: {src_uid!r} -> {dst_uid!r}")
        # Monotonic confidence: a re-upsert never LOWERS confidence, and weight/source follow the
        # higher-confidence claim. This lets a precise extractor (TD-005) supersede a coarse edge
        # without a later coarse pass downgrading it. `edges` = existing row, `excluded` = new row.
        self.conn.execute(
            "INSERT INTO edges(src, dst, relation, weight, confidence, source) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(src, dst, relation) DO UPDATE SET "
            # Strict '>' : a STRICTLY higher-confidence claim takes weight/source; an equal-confidence
            # re-upsert keeps the existing provenance, so a same-run re-resolve (or a coarse pass on a
            # tie) can't clobber a precise edge's source (R7-3).
            "  weight  = CASE WHEN excluded.confidence > edges.confidence THEN excluded.weight ELSE edges.weight END, "
            "  source  = CASE WHEN excluded.confidence > edges.confidence THEN excluded.source ELSE edges.source END, "
            "  confidence = MAX(edges.confidence, excluded.confidence)",
            (s, d, relation, weight, confidence, source),
        )
        # Graph-aware embedding staleness (TD-006): both endpoints' neighborhoods changed.
        self.conn.execute("UPDATE nodes SET embed_dirty = 1 WHERE id IN (?, ?)", (s, d))

    def set_embedding(self, node_id: int, vector: Sequence[float], model: Optional[str] = None) -> None:
        unit = normalize(vector)   # store unit-normalized so query-time cosine is a dot product (MR-4)
        self.conn.execute(
            "INSERT INTO embeddings(node_id, dim, vector, model) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET dim=excluded.dim, vector=excluded.vector, model=excluded.model",
            (node_id, len(unit), pack(unit), model),
        )
        self.conn.execute("UPDATE nodes SET embed_dirty = 0 WHERE id = ?", (node_id,))
        # Keep a derived ANN index in sync (vec0). The BLOB above is authoritative and already written,
        # so a sync failure is logged, not raised — rebuild_index() is the backstop (sqlite-vec spec).
        idx = self._index
        if idx is not None and hasattr(idx, "upsert"):
            try:
                idx.upsert(node_id, unit)
            except Exception:   # noqa: BLE001 - never let a derived-index hiccup break the authoritative write
                logging.getLogger(__name__).debug("vec index upsert failed for node %s", node_id, exc_info=True)

    # --- reads -------------------------------------------------------------
    def get_nodes(self, ids: Sequence[int]) -> list[dict]:
        if not ids:
            return []
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE id IN (SELECT value FROM json_each(:ids))",
            {"ids": json.dumps([int(i) for i in ids])},
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def dirty_nodes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM nodes WHERE embed_dirty = 1").fetchall()
        return [self._row_to_node(r) for r in rows]

    def dirty_node_ids(self) -> list[int]:
        """Just the ids of stale nodes (uses idx_nodes_dirty). The embedding pipeline streams these in
        batches and fetches each batch's full rows lazily, so peak memory is O(batch) not O(corpus)
        with full bodies (perf MR-5)."""
        return [r[0] for r in self.conn.execute("SELECT id FROM nodes WHERE embed_dirty = 1")]

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["attrs"] = json.loads(d["attrs"]) if d["attrs"] else {}
        return d
