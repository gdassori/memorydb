---
id: TD-004
title: "Zero-dependency core: pure-Python brute-force vectors by default, sqlite-vec as an optional accelerator"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [vectors, sqlite-vec, dependencies, embedded]
---

# TD-004: Zero-dependency core; brute-force vectors default, sqlite-vec optional

## Context

The environment had **no third-party libraries installed**, and `sqlite3` already reports `enable_load_extension: True`. Embedded scale for a codebase or personal memory is thousands to low-millions of vectors, not billions.

## Decision

The **core depends only on the Python standard library**. The default `VectorIndex` is **pure-Python brute-force cosine** over `float32` BLOBs ([../../src/memorydb/vector.py](../../src/memorydb/vector.py)). `sqlite-vec` is an **optional `[vector]` extra**, a drop-in accelerator behind the *same* `VectorIndex` interface. `tree-sitter` is an optional `[code]` extra; `networkx` an optional `[graph]` extra.

## Rationale

The package **runs and passes its tests out of the box** with no installs — the right posture for an embedded library. Brute-force cosine is *exact* and perfectly fast at embedded scale; graceful degradation beats a hard native dependency. The interface boundary lets us swap in ANN (`sqlite-vec`) later without touching any caller.

## Consequences

- **Positive:** no install friction, no native build to ship a working v0; trivial to test with fakes; honest performance story (exact now, accelerated later).
- **Negative:** brute force is O(n) per query — fine to ~10⁵ vectors. Beyond that, install the `[vector]` extra and switch to the `sqlite-vec` index.

## Alternatives Considered

### Hard-depend on sqlite-vec from day one
Rejected: install friction and a native extension on the critical path break "works out of the box".

### FAISS / hnswlib as the vector backend
Rejected for v0: heavy, native builds, oriented at large-scale ANN — over-engineered for embedded scale and against the zero-dep goal.
