---
title: "<concise feature/component name>"
status: planned            # planned | active | completed | rejected
created: 2026-06-22
author: claude
related_tds: [TD-00X, TD-00Y]
components: [<package modules this touches, e.g. store, query, adapters/code>]
---

# <Title>

> One-paragraph abstract: what this builds, for whom, and the single most important
> design constraint. Link the governing TD(s).

## Goal

What "done" means, in 3–6 sentences. State the **definition of done** as an observable
behavior (a query that works, a test that passes, a CLI that runs), not "code exists".

## Background & constraints

Why this is shaped the way it is. Reference the TDs that bind it, the v0 interfaces it must
respect, and any non-negotiable constraints (zero-dep core, embedded scale, stdlib-first).

## Approach

The chosen design in prose. Name the key modules/classes/functions and how data flows
through them. Call out where it plugs into the existing substrate (Store / query / ports).

## Data model & interfaces

Concrete signatures and types — the contract, before the prose. Prefer real Python:

```python
# new/changed public surface
class Foo:
    def method(self, x: Bar) -> Baz: ...
```

SQL / schema changes (if any), as DDL:

```sql
-- migrations or new tables/indexes
```

## Algorithm / step-by-step

The core logic as numbered steps or pseudocode. Be explicit about ordering, batching,
transactions, and what is idempotent. Include at least one worked example with concrete
inputs → outputs.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/...` | **New / Modify / Remove** — one line |

## Edge cases & failure modes

Bullet list. For each: the condition, the expected behavior, and how we detect it. Cover
empty input, missing dependency (optional extra), concurrent writes, malformed data,
ambiguity (e.g. coarse-edge false positives), and resource bounds.

## Test plan

Concrete tests (name → asserts). Must be runnable with the zero-dep core where possible;
note any that require an optional extra. Include the smallest reproduction of the happy
path plus the riskiest edge case.

## Performance & scale

Expected complexity, the scale ceiling, and the mitigation/upgrade path (e.g. brute-force →
sqlite-vec). State numbers where known (vector counts, node counts, latency targets).

## Tasks

- [ ] ordered, checkable units of work — each independently verifiable
- [ ] ...

## Open questions

- Genuine unknowns with a leaning ("Lean X because …"), not rhetorical questions.

## Risks

- Risk → mitigation, one line each. Be honest about what could make this the wrong design.

## References

- TDs, prior specs, external libs/docs, prior art.
