"""Schema versioning & forward migrations (TD-003, schema-migrations spec).

`PRAGMA user_version` is the schema version. `migrate()` applies every migration with a version
greater than the DB's current one, in order, each in its own transaction, bumping `user_version`.
Forward-only: opening a DB newer than this build raises rather than corrupting data.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# Migration 1's body. Pure DDL (no connection pragmas), so it can run statement-by-statement
# inside the per-migration transaction (executescript would auto-commit and break atomicity).
_BASELINE = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Migration:
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


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", _m1_baseline),
    Migration(2, "meta", _m2_meta),
    # Future (documented in specs, not yet coded):
    #   3: node_history / edge_history (TD-009 temporal identity)
    #   4: vec0 ensure (sqlite-vec, created lazily at the known embedding dim — C3)
    #   5: concepts / concept_edges (concept-ontology-layer)
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
