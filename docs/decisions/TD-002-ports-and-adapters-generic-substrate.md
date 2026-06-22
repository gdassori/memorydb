---
id: TD-002
title: "Ports-and-adapters: a domain-agnostic substrate, with Code and Memory as adapters"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [architecture, hexagonal, substrate, adapters]
---

# TD-002: Ports-and-adapters — a generic substrate, Code/Memory as adapters

## Context

Two candidate products share the same storage substrate: a **Code Memory Engine** (index a codebase into a symbol graph) and an **Agent Memory** ("external brain": entities, episodic/semantic/procedural facts). The maintainer chose to build the substrate as a **generic, domain-agnostic core** rather than starting code-first.

## Decision

The **core** knows only generic `Node` / `Edge` / `Vector` and how to compose vector + graph + relational retrieval. All domain knowledge lives in **adapters** that map their concepts onto `Node`/`Edge`. Three injectable **ports** (`typing.Protocol`): `Embedder`, `IntentClassifier`, `Extractor`. `CodeAdapter` and `MemoryAdapter` are *consumers* of the core, never part of it.

## Rationale

One substrate, two products, clean seams. The inference framework owns the embedding models, so the substrate must **receive** an `Embedder`, not contain one. Building generic-first keeps both products honest about what is shared vs. domain-specific. Honest trade-off: substrate-first is the **slowest to show visible value** — mitigated by shipping `CodeAdapter` alongside the v0 substrate ([specs/active/v0-substrate.md](../specs/active/v0-substrate.md)).

## Consequences

- **Positive:** the Agent-Memory product later is "just another adapter", not a rewrite; ports make the embedder/classifier/extractor swappable and testable with fakes.
- **Negative:** a purely generic core risks being a thin SQLite wrapper — it must earn its keep via the retrieval planner ([TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md)) and embedding-staleness orchestration ([TD-006](TD-006-graph-aware-embeddings-staleness.md)), not just CRUD.

## Alternatives Considered

### Code-first monolith, generalize later
Rejected by the maintainer: would force a rework when the memory product arrives, and bakes code assumptions into the core.

### Fully generic core with no adapter shipped in v0
Rejected: maximal cleanliness but zero visible value; we ship the code adapter on top from day one.
