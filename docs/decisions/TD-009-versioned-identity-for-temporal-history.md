---
id: TD-009
title: "Temporal history lives in separate history tables; nodes/edges keep a UNIQUE current version"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [temporal, schema, identity, adversarial-review]
---

# TD-009: Versioned identity via history tables, not duplicate uids

## Context

The adversarial review ([../specs/adversarial-review-2026-06-22.md](../specs/adversarial-review-2026-06-22.md),
finding C1) showed that the temporal design ([temporal-confidence-machinery.md](../specs/active/temporal-confidence-machinery.md))
is impossible against the v0 schema: it wanted to keep multiple time-versions of a fact by inserting a new row with
the **same `uid`**, but `nodes.uid` is `UNIQUE` (verified: a duplicate-`uid` insert raises `IntegrityError`). This
also partly falsified [TD-008](TD-008-defer-temporal-confidence-ontology-reflection.md)'s premise that the *reserved
columns alone* make temporal cheap — temporal needs a **schema/identity** decision, made here.

## Decision

`nodes` and `edges` **always hold the single current (open) version**, with **`uid UNIQUE` unchanged**. Closed
versions are archived to dedicated history tables:

```sql
CREATE TABLE node_history (
    node_id     INTEGER NOT NULL,   -- the live nodes.id this is a past version of
    uid         TEXT NOT NULL,
    type TEXT, name TEXT, body TEXT, attrs TEXT, source TEXT,
    valid_from  TEXT, valid_to TEXT NOT NULL,   -- closed interval
    confidence  REAL
);
CREATE INDEX idx_node_history_uid ON node_history(uid, valid_to);
-- edge_history mirrors this for relations.
```

**Supersede** = within one transaction: copy the current `nodes` row into `node_history` with `valid_to = at`, then
**UPDATE the live row in place** (new body/attrs, `valid_from = at`, `valid_to = NULL`). **`as_of(t)`** = the live row
when `valid_from <= t`, else the matching `node_history` row whose `[valid_from, valid_to)` contains `t`.

## Rationale

The single-table alternative (allow duplicate `uid`s, gate uniqueness with a partial index on open rows) would force
**every existing query** to add `WHERE valid_to IS NULL` or risk reading stale versions — a tax on the whole codebase
([TD-003](TD-003-sqlite-single-store-recursive-cte.md) queries, the planner, embeddings). History tables keep the hot
path identical and make temporal **fully opt-in**: code retrieval never pays for it, and v0 needs **no change now** (the
history tables arrive in a migration when temporal lands, [schema-migrations.md](../specs/completed/schema-migrations.md)).

## Consequences

- **Positive:** `uid UNIQUE` and all current queries are untouched; temporal cost is isolated to history tables and the
  `as_of`/`supersede` paths; v0 stays as-is.
- **Negative:** `as_of` must union live + history (a small query cost); history tables grow (bounded by pruning in the
  reflection daemon, [reflection-daemon.md](../specs/active/reflection-daemon.md)).

## Alternatives Considered

### Duplicate `uid`s + partial unique index on open rows (`WHERE valid_to IS NULL`)
Rejected: elegant in one table, but taxes every current-version query with a `valid_to IS NULL` filter and risks subtle
"saw a stale version" bugs across the codebase.

### Versioned uids (`uid#vN`)
Rejected: breaks the stable-identity contract the code adapters rely on (uid = `relpath::qualname`) and complicates joins.

## References

- Corrects [temporal-confidence-machinery.md](../specs/active/temporal-confidence-machinery.md) and amends
  [TD-008](TD-008-defer-temporal-confidence-ontology-reflection.md).
- [../specs/adversarial-review-2026-06-22.md](../specs/adversarial-review-2026-06-22.md) (C1).
