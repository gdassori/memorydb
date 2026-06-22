-- MemoryDB substrate schema (TD-003) — the baseline applied by migration 1 (schema-migrations spec).
-- Metadata columns (source/valid_from/valid_to/confidence) are present but mostly unused in v0 (TD-008).
-- Connection pragmas (foreign_keys, journal_mode) are set by Store.__init__, not here, so this file is
-- pure DDL that the migration runner can apply statement-by-statement inside a transaction.

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,
    uid         TEXT NOT NULL UNIQUE,        -- stable external id (e.g. fully-qualified symbol name)
    type        TEXT NOT NULL,              -- Function / Class / Entity / Concept / ...
    name        TEXT NOT NULL,
    body        TEXT,                       -- text shown / serialized for embedding
    attrs       TEXT,                       -- JSON, adapter-specific
    source      TEXT,                       -- provenance (TD-008)
    valid_from  TEXT,                       -- temporal validity (TD-008, unused in v0)
    valid_to    TEXT,
    confidence  REAL NOT NULL DEFAULT 1.0,
    embed_dirty INTEGER NOT NULL DEFAULT 1  -- staleness flag for graph-aware embeddings (TD-006)
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    src         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,              -- CALLS / IMPORTS / INHERITS / WRITES / IMPLEMENTED_BY / ...
    weight      REAL NOT NULL DEFAULT 1.0,
    confidence  REAL NOT NULL DEFAULT 1.0,  -- heuristic/coarse edges get < 1.0 (TD-005)
    source      TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    UNIQUE(src, dst, relation)
);

CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, relation);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, relation);

CREATE TABLE IF NOT EXISTS embeddings (
    node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    dim     INTEGER NOT NULL,
    vector  BLOB NOT NULL,                  -- packed float32 (TD-004)
    model   TEXT
);
