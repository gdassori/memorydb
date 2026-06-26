---
id: TD-010
title: "Domain models are pydantic BaseModels (and str-Enums), not dataclasses"
status: accepted
date: 2026-06-22
supersedes: null
superseded_by: null
tags: [models, pydantic, dependencies, ergonomics]
---

# TD-010: pydantic domain models (and str-Enums) over dataclasses

## Context

The substrate's domain types (`Node`, `Edge`, the `*Report`s, `EvalCase`/`Scorecard`, `ContextResult`,
`IntentResult`, `LangSpec`, `Migration`, …) started as stdlib `@dataclass`es under the original
"core depends only on the standard library" rule ([TD-004](TD-004-zero-dep-core-bruteforce-vectors.md)).
As the model surface grew (validation of LLM-supplied data, ranges, enums, serialisation for the API and
CLI), hand-rolled `__post_init__` validation and ad-hoc `to_dict` became the friction that a schema layer
exists to remove.

## Decision

**All domain models are `pydantic.BaseModel`s (pydantic v2), constructed keyword-only.** Constant
namespaces that were class-of-constants (`Rel`, `Intent`) are **`str`-`Enum`s** (so they serialise as
their string value and compare equal to it). `pydantic` (>=2) is therefore the project's **one** core
runtime dependency — this is the dependency relaxation recorded in
[TD-004](TD-004-zero-dep-core-bruteforce-vectors.md) (which keeps ownership of the *vectors / zero-dep*
posture; this TD owns the *models* decision). Models that hold a budget/score/confidence validate their
ranges (`Field(ge=…, le=…)`); models handed back from a cache are `frozen` where mutation would corrupt
shared state (e.g. `IntentResult`).

## Rationale

- **Validation at the boundary.** LLM- and repo-supplied data (intent JSON, filter dicts, attrs) is
  validated/coerced once, at construction, instead of scattered manual checks — see the FILTER/intent work
  ([TD-007](TD-007-intent-routed-retrieval-tj-is-orchestration.md)).
- **Ergonomics.** Free `__init__`/`__repr__`/`__eq__`, `model_copy(update=…)` for the immutable-update
  pattern, and `model_dump()` for the API/CLI JSON surface.
- **Ports stay clean** ([TD-002](TD-002-ports-and-adapters-generic-substrate.md)): models are plain data
  across the port boundary; pydantic is an implementation detail of the data types, not of the Protocols.

## Consequences

- **Positive:** one validation/serialisation idiom everywhere; keyword-only construction prevents
  positional-arg drift as models grow; `frozen` + `model_copy` give cheap, safe immutability.
- **Negative:** the package no longer runs in a *completely empty* environment — `pydantic` must be
  installed, and pydantic v2 pulls in **`pydantic-core`, a compiled (Rust) wheel** (MR-23): prebuilt wheels
  exist for common platforms, but the core is no longer pure Python. The absolute zero-install property is
  traded for validation ergonomics on an already `pip install`-ed library. Brute-force cosine stays the
  default `VectorIndex` and `sqlite-vec`/`tree-sitter`/`networkx` remain optional extras — unchanged.

## Alternatives Considered

### Keep stdlib `@dataclass` + manual validation
Rejected: the validation/serialisation we kept re-implementing (ranges, enum coercion, JSON in/out) is
exactly pydantic's job; hand-rolling it is more code and more bugs for less safety.

### `attrs` / `msgspec`
Rejected for v0: `attrs` still needs a separate validation story; `msgspec` is leaner but less ubiquitous
and its ergonomics/ecosystem are a worse fit than pydantic v2 for a library others embed.

## References

- [TD-004](TD-004-zero-dep-core-bruteforce-vectors.md) (origin of the dependency relaxation; vectors/zero-dep posture),
  [TD-002](TD-002-ports-and-adapters-generic-substrate.md) (ports & adapters).
- `pyproject.toml` (`dependencies = ["pydantic>=2"]`).
