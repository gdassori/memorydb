---
title: "Schema versioning & forward migrations"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-003]
components: [store, schema]
---

# Schema versioning & forward migrations

> Replace the v0 "run `schema.sql` at init" with a versioned, forward-only migration system keyed on
> `PRAGMA user_version`, so the single SQLite store ([TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md))
> can evolve (vec0 table, concepts tables, new columns) without data loss.

## Goal

Opening any MemoryDB file applies all pending migrations in order, transactionally, and bumps
`user_version`. Done = a fresh DB ends at the latest version; an old (v0) DB migrates cleanly; a DB
newer than the running code raises a clear error instead of corrupting data.

## Background & constraints

v0 calls `conn.executescript(schema.sql)` in `Store.__init__`. That is fine for a single schema but has
no upgrade path. SQLite's `PRAGMA user_version` is a free integer in the DB header — the canonical place
to store schema version. Forward-only keeps it simple (no down-migrations); embedded single-file means
no central migration coordinator.

## Approach

A `MIGRATIONS` list of `Migration(version, apply)` where `apply(conn)` runs DDL/Python. `Store.__init__`
reads `user_version`, runs every migration with `version > current` in ascending order (each in its own
transaction), then sets `user_version` to the last applied. The current `schema.sql` becomes the body of
migration **1** (the v0 baseline).

## Data model & interfaces

```python
from dataclasses import dataclass
from typing import Callable
import sqlite3

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

MIGRATIONS: list[Migration] = [ ... ]   # ordered, contiguous from 1
LATEST = MIGRATIONS[-1].version

def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > LATEST:
        raise RuntimeError(f"DB schema v{current} is newer than this build (v{LATEST}); upgrade memorydb")
    for m in MIGRATIONS:
        if m.version > current:
            with conn:                       # transaction per migration
                m.apply(conn)
                conn.execute(f"PRAGMA user_version = {m.version}")
    return LATEST
```

`Store.__init__` calls `migrate(self.conn)` instead of `executescript(schema.sql)`.

## Algorithm / step-by-step

1. Read `user_version` (0 for a brand-new or pre-migration DB).
2. If `> LATEST` → raise (forward-only; never downgrade).
3. For each migration with `version > current`: run `apply(conn)` and set `user_version` **in the same
   transaction** (atomic: either both happen or neither).
4. Return `LATEST`.

**Migration 1 (baseline)** = today's `schema.sql` (nodes/edges/embeddings + indexes).
**Migration 2 (example, vec0)** — only if sqlite-vec is loaded (coordinate with
[sqlite-vec-acceleration.md](sqlite-vec-acceleration.md)):
```python
def m2_vec0(conn):
    if extension_available(conn):
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(node_id integer primary key, embedding float[768])")
    # else: no-op now; a later 'ensure_vec0' runs when the extension is present
```
**Migration 3 (example, concepts)** — `concepts`, `concept_edges` tables for
[concept-ontology-layer.md](concept-ontology-layer.md).

## What changes

| File | Change |
|------|--------|
| `src/memorydb/migrations.py` | **New** — `Migration`, `MIGRATIONS`, `migrate()` |
| `src/memorydb/store.py` | **Modify** — `__init__` calls `migrate()` instead of `executescript(schema.sql)` |
| `src/memorydb/schema.sql` | **Keep** as the literal body of migration 1 (single source for the baseline) |

## Edge cases & failure modes

- **Fresh DB:** `user_version=0` → all migrations run → ends at `LATEST`.
- **Crash mid-migration:** per-migration transaction rolls back; `user_version` unchanged → safe re-run.
- **Newer DB than code:** raise with an actionable message.
- **Concurrent openers:** SQLite write lock serializes; the second opener sees the bumped version and skips.
- **Extension-dependent step absent:** make it a no-op now + an idempotent "ensure" step that runs once the
  extension appears (so enabling `[vector]` later still creates vec0).

## Test plan

Zero-dep:

- `test_fresh_db_at_latest` — new `:memory:` Store → `user_version == LATEST`.
- `test_migrates_v0_db` — create a DB with only the v0 schema + `user_version=1`, add a later migration,
  reopen → applied, version bumped, existing rows intact.
- `test_rejects_newer_db` — set `user_version = LATEST+1` → `Store(...)` raises.
- `test_partial_failure_rolls_back` — a migration that raises mid-way → `user_version` unchanged, DB usable.

## Performance & scale

Migrations run once per version bump at open time; negligible. Large data migrations (e.g. backfilling a
new column) should be batched and are noted per-migration; none required for the baseline.

## Tasks

- [ ] `migrations.py` with `migrate()` + baseline migration 1 (= schema.sql)
- [ ] switch `Store.__init__` to `migrate()`
- [ ] `extension_available()` helper + idempotent vec0 "ensure" pattern
- [ ] zero-dep tests (fresh / upgrade / reject-newer / rollback)

## Open questions

- Embed baseline DDL as a Python string vs read `schema.sql`? **Lean** keep `schema.sql` as the file and
  have migration 1 read+exec it, so there is one baseline source.
- Record a migration history table (audit) vs only `user_version`? **Lean** `user_version` only for v1;
  add a history table if debugging upgrades becomes painful.

## Risks

- **A migration that is not idempotent** corrupts on partial failure → enforce transaction-per-migration +
  `IF NOT EXISTS` DDL.
- **Forgetting to bump `LATEST`** when adding a migration → assert contiguity of versions in a test.

## References

- [TD-003](../../decisions/TD-003-sqlite-single-store-recursive-cte.md)
- [sqlite-vec-acceleration.md](sqlite-vec-acceleration.md), [concept-ontology-layer.md](concept-ontology-layer.md)
- SQLite `PRAGMA user_version`.
