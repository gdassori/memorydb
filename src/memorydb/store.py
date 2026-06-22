"""The Store: a SQLite connection + schema + write/read API (TD-003).

Single source of truth for nodes, edges and embeddings. Maintains the ``embed_dirty``
staleness flag for graph-aware embeddings (TD-006): upserting an edge marks both
endpoints dirty, since their serialized neighborhoods changed.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional, Sequence

from .migrations import migrate
from .models import Node
from .vector import pack


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Connection pragmas live here (not in schema.sql) so the schema stays pure DDL.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        migrate(self.conn)  # apply pending schema migrations (TD-003 / schema-migrations spec)

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
            "  weight  = CASE WHEN excluded.confidence >= edges.confidence THEN excluded.weight ELSE edges.weight END, "
            "  source  = CASE WHEN excluded.confidence >= edges.confidence THEN excluded.source ELSE edges.source END, "
            "  confidence = MAX(edges.confidence, excluded.confidence)",
            (s, d, relation, weight, confidence, source),
        )
        # Graph-aware embedding staleness (TD-006): both endpoints' neighborhoods changed.
        self.conn.execute("UPDATE nodes SET embed_dirty = 1 WHERE id IN (?, ?)", (s, d))

    def set_embedding(self, node_id: int, vector: Sequence[float], model: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO embeddings(node_id, dim, vector, model) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET dim=excluded.dim, vector=excluded.vector, model=excluded.model",
            (node_id, len(vector), pack(vector), model),
        )
        self.conn.execute("UPDATE nodes SET embed_dirty = 0 WHERE id = ?", (node_id,))

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

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["attrs"] = json.loads(d["attrs"]) if d["attrs"] else {}
        return d
