---
id: TD-005
title: "Multi-language indexing via tree-sitter; coarse name-based edges carry low confidence"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [ast, tree-sitter, multilang, symbol-resolution, confidence]
---

# TD-005: Multi-language via tree-sitter; coarse edges carry low confidence

## Context

The maintainer chose **multi-language indexing from day one** (Python, Go, Rust, JS/TS, …). `tree-sitter` gives a uniform CST across ~165 languages via **`tree-sitter-language-pack`** (precompiled grammars, no per-grammar build). But tree-sitter does **not** do symbol resolution: a call node `foo()` is just the text `"foo"`; resolving *which* `foo` (across imports, methods, overloads, shadowing) is language-specific and is the genuinely hard part.

## Decision

Extract **nodes** (symbols: function/class/method/import) and **coarse, name-based edges** with tree-sitter across all languages. Tag every heuristic edge with **`confidence` < 1.0** (scoped by file/import where possible). Precise per-language resolvers (e.g. Python `ast` + `symtable`, or an LSP) arrive later as **higher-confidence `Extractor`s** behind the same port ([TD-002](TD-002-ports-and-adapters-generic-substrate.md)).

## Rationale

Breadth now, precision incrementally. This is where the `confidence` column (already in the schema) **earns its keep in v0**: the retrieval planner can downweight heuristic edges and prefer high-confidence ones for `LOCATE` ([TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md)). It also keeps the deterministic-graph thesis honest — we never present a guessed edge as ground truth.

## Consequences

- **Positive:** day-one coverage of many languages with one extractor; a clean upgrade path (drop in a precise resolver per language without touching callers).
- **Negative:** multilang edges have false positives; `LOCATE` on a coarse-only language is **best-effort** until a precise resolver lands. This weakens the "compiler-knows-exactly" promise for those languages — we log/flag it rather than hide it.

## Alternatives Considered

### Python-only first, with jedi/pyright for precise edges
Rejected by the maintainer in favor of breadth. (It would give the best graph fastest, but only for one language.)

### Build each tree-sitter grammar by hand
Rejected: `tree-sitter-language-pack` ships precompiled grammars — hand-building is the real pain we avoid.
