"""Schema versioning & forward migrations (TD-003, schema-migrations spec).

`PRAGMA user_version` is the schema version. `migrate()` applies every migration with a version
greater than the DB's current one, in order, each in its own transaction, bumping `user_version`.
Forward-only: opening a DB newer than this build raises rather than corrupting data.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Optional, Sequence

from pydantic import BaseModel, ConfigDict

# Migration 1's body. Pure DDL (no connection pragmas), so it can run statement-by-statement
# inside the per-migration transaction (executescript would auto-commit and break atomicity).
_BASELINE = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


class Migration(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _exec_script(conn: sqlite3.Connection, sql: str) -> None:
    """Run a pure-DDL script one statement at a time (keeps the enclosing transaction intact)."""
    for stmt in sql.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _m1_baseline(conn: sqlite3.Connection) -> None:
    _exec_script(conn, _BASELINE)  # nodes / edges / embeddings + indexes (idempotent: IF NOT EXISTS)


def _m2_meta(conn: sqlite3.Connection) -> None:
    # Key/value store for substrate metadata (e.g. the embedding dim that vec0 needs — C3).
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def _m3_file_uid_index(conn: sqlite3.Connection) -> None:
    # An indexable handle on attrs.file_uid: a VIRTUAL generated column (computed, not stored) plus an
    # index, so deleting a file's symbols is an indexed lookup instead of a full json_extract scan
    # (perf I8). Also a partial index on the staleness flag so refresh() finds the dirty set without
    # scanning every node (perf I12).
    # ALTER ... ADD COLUMN has no IF NOT EXISTS — guard it so a re-run / racing first-open doesn't fail
    # with 'duplicate column name' (MR-18). table_xinfo lists VIRTUAL generated columns.
    cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(nodes)")}
    if "file_uid" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN file_uid TEXT "
            "GENERATED ALWAYS AS (json_extract(attrs, '$.file_uid')) VIRTUAL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_file_uid ON nodes(file_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_dirty ON nodes(embed_dirty) WHERE embed_dirty = 1")


def _m4_pending_edges(conn: sqlite3.Connection) -> None:
    # Durable store of unresolved/coarse by-name edges (src_uid --relation--> dst_name). Persisting
    # them lets the indexer re-resolve a caller's reference whenever the callee's name appears or
    # disappears in ANY file, not only when the caller file itself is re-extracted — otherwise editing
    # a callee file cascade-deletes the cross-file edge and it is never rebuilt (data-integrity R3L-1).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_edges ("
        "  src_uid    TEXT NOT NULL,"
        "  src_file   TEXT NOT NULL,"   # the relpath that emitted it, so re-indexing a file can clear its rows
        "  dst_name   TEXT NOT NULL,"
        "  relation   TEXT NOT NULL,"
        "  confidence REAL NOT NULL DEFAULT 0.3,"
        "  PRIMARY KEY (src_uid, dst_name, relation))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_dst_name ON pending_edges(dst_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_src_file ON pending_edges(src_file)")


def _m5_pending_dst_uid(conn: sqlite3.Connection) -> None:
    # dst_uid: the exact resolved dst uid for a PRECISE cross-file edge, so re-resolution after a callee
    # edit rebuilds the same edge even when the target name is a duplicate qualname (R6-2). source: the
    # edge's true provenance, so a precise edge rebuilt via pending keeps e.g. 'python-ast' instead of a
    # hardcoded 'treesitter' (R7-3). Both NULL for coarse by-name rows.
    cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(pending_edges)")}
    if "dst_uid" not in cols:
        conn.execute("ALTER TABLE pending_edges ADD COLUMN dst_uid TEXT")
    if "source" not in cols:
        conn.execute("ALTER TABLE pending_edges ADD COLUMN source TEXT")


MIGRATIONS: list[Migration] = [
    Migration(version=1, name="baseline", apply=_m1_baseline),
    Migration(version=2, name="meta", apply=_m2_meta),
    Migration(version=3, name="file_uid_index", apply=_m3_file_uid_index),
    Migration(version=4, name="pending_edges", apply=_m4_pending_edges),
    Migration(version=5, name="pending_dst_uid", apply=_m5_pending_dst_uid),
    # Future (documented in specs, not yet coded):
    #   6: node_history / edge_history (TD-009 temporal identity)
    #   7: vec0 ensure (sqlite-vec, created lazily at the known embedding dim — C3)
    #   8: concepts / concept_edges (concept-ontology-layer)
]
LATEST = MIGRATIONS[-1].version


def migrate(conn: sqlite3.Connection, migrations: Optional[Sequence[Migration]] = None) -> int:
    """Apply pending migrations in order; return the resulting schema version.

    Each migration + its `user_version` bump run in one transaction: a failure rolls that migration
    back (and re-running is safe), so a crash never leaves a half-applied version.
    """
    migrations = list(migrations) if migrations is not None else MIGRATIONS
    latest = migrations[-1].version if migrations else 0
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > latest:
        raise RuntimeError(
            f"DB schema v{current} is newer than this build (v{latest}); upgrade memorydb"
        )
    for m in migrations:
        if m.version > current:
            # Explicit BEGIN: Python's sqlite3 only auto-opens a transaction for DML, not DDL, so
            # without this a CREATE TABLE would autocommit and survive a rollback. There is no open
            # transaction at migrate() time (called right after connect), so BEGIN is safe.
            conn.execute("BEGIN")
            try:
                m.apply(conn)
                conn.execute(f"PRAGMA user_version = {int(m.version)}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return latest
