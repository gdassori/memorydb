"""Tests for the schema-migrations system (TD-003). Zero third-party deps."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import Store  # noqa: E402
from memorydb.migrations import LATEST, MIGRATIONS, Migration, migrate  # noqa: E402


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def test_fresh_db_is_at_latest():
    s = Store(":memory:")
    assert _user_version(s.conn) == LATEST
    assert _has_table(s.conn, "nodes")
    assert _has_table(s.conn, "meta")  # migration 2


def test_migrations_versions_are_contiguous():
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1))


def test_migrates_from_v0():
    # A pre-migration DB: baseline present, user_version still 0.
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version").fetchone()
    assert _user_version(conn) == 0
    migrate(conn)
    assert _user_version(conn) == LATEST
    assert _has_table(conn, "nodes") and _has_table(conn, "meta")
    # Idempotent: a second run is a no-op.
    assert migrate(conn) == LATEST


def test_rejects_newer_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA user_version = {LATEST + 1}")
    try:
        migrate(conn)
        assert False, "expected RuntimeError for a newer-than-build DB"
    except RuntimeError as e:
        assert "newer" in str(e)


def test_partial_failure_rolls_back():
    def good(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS m_ok (id INTEGER PRIMARY KEY)")

    def bad(conn):
        conn.execute("CREATE TABLE m_bad (id INTEGER PRIMARY KEY)")
        raise RuntimeError("boom")  # after a write, before the version bump

    conn = sqlite3.connect(":memory:")
    migs = [Migration(1, "good", good), Migration(2, "bad", bad)]
    try:
        migrate(conn, migrations=migs)
        assert False, "expected the failing migration to raise"
    except RuntimeError:
        pass
    # Migration 1 committed (version 1); migration 2 rolled back (its table is gone).
    assert _user_version(conn) == 1
    assert _has_table(conn, "m_ok")
    assert not _has_table(conn, "m_bad")


def test_persists_across_reopen():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "mem.db")
        s1 = Store(path)
        from memorydb import Node, Rel
        s1.upsert_node(Node(uid="x", type="function", name="x"))
        s1.upsert_node(Node(uid="y", type="function", name="y"))
        s1.upsert_edge("x", "y", Rel.CALLS)
        s1.commit()
        s1.close()
        # Reopen: migrations are a no-op, data survives.
        s2 = Store(path)
        assert _user_version(s2.conn) == LATEST
        assert s2.id_for("x") is not None
        s2.close()


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        fn()
        print(f"ok  {name}")
    print(f"\nall green ({len(tests)} tests)")
