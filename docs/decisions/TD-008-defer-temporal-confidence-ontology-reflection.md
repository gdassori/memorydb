---
id: TD-008
title: "Defer the temporal/confidence machinery, the concept/ontology layer, and reflection — keep the columns"
status: proposed
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [scope, deferred, temporal, ontology, reflection]
---

# TD-008: Defer temporal/confidence machinery, ontology, and reflection

## Context

The brainstorm proposed a rich memory model: temporal validity (`valid_from`/`valid_to`), confidence calibration and decay, provenance, an auto-extracted **concept/ontology** layer, and nightly **reflection** that synthesizes new concepts from clusters. All are compelling; all are either a **tax on every write** or an open **research project**.

## Decision

Put the **columns** in the schema now — `valid_from`, `valid_to`, `confidence`, `source` on both `nodes` and `edges` (cheap, future-proof) — but **defer the machinery**: no temporal-logic queries, no confidence decay/calibration, no concept extraction, no reflection in v0. In v0, `confidence` is used **only** as a static heuristic weight for coarse edges ([TD-005](TD-005-multilang-treesitter-coarse-edges-confidence.md)).

## Rationale

Avoid building unbounded research machinery before there is a use case to evaluate it against. Schema-readiness keeps every door open at near-zero cost (no painful migration later). **Reflection especially is a north-star** — an agent that grows its own ontology — not a v0 feature.

## Consequences

- **Positive:** v0 stays shippable, testable, and evaluable; the metadata model is ready the day a use case appears.
- **Negative:** features sketched in the brainstorm are explicitly **not present yet** — this TD exists so nobody assumes otherwise.

## Review note (2026-06-22)

Correction: the claim that the *reserved columns alone* make temporal cheap is only partly true. The adversarial
review (C1) showed the bitemporal model collides with `nodes.uid UNIQUE`, so temporal needs a small
**schema/identity** decision — now made in [TD-009](TD-009-versioned-identity-for-temporal-history.md) (history
tables; the current row keeps its UNIQUE uid). The columns remain useful day-one (static `confidence` weighting),
but "just columns" is not sufficient for the temporal machinery itself.

## Alternatives Considered

### Build the full memory model (temporal + confidence + ontology + reflection) now
Rejected: scope explosion, no evaluable v0, and most of it is research, not engineering.

### Omit the metadata columns until needed
Rejected: re-adding `valid_from`/`confidence`/`source` later means data migrations across every adapter — cheaper to carry the columns from the start.
