---
title: "LLM intent classifier & the FILTER path"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-007, TD-002]
components: [planner, ports]
---

# LLM intent classifier & the FILTER path

> Replace the regex `DefaultIntentClassifier` with an injectable LLM router that returns a structured
> intent + extracted symbol/entities + a FILTER predicate, and implement the currently-stubbed `FILTER`
> path in the planner ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).

## Goal

`LLMIntentClassifier(client).classify(query)` returns a validated `IntentResult`; the `RetrievalPlanner`
routes on it, and `FILTER` produces real, safe SQL. Done = the three example queries below route correctly,
the FILTER SQL is parameterized (no injection), and any LLM failure falls back to the regex classifier.

## Background & constraints

Vectors only as GPS; determinism where it exists ([TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md)).
The classifier is a port ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)) — the LLM
**client is injected**, never hardcoded (the local framework defaults to Claude/Anthropic models, but this
module must not import a provider). Cost matters: the prompt is tiny and cached.

## Data model & interfaces

```python
from typing import Protocol
from dataclasses import dataclass, field
from memorydb.models import Intent

class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...   # returns model text (expected JSON)

@dataclass
class IntentResult:
    intent: Intent
    symbol: str | None = None
    entities: list[str] = field(default_factory=list)
    filters: dict = field(default_factory=dict)   # {type?, lang?, path_glob?, since?}
    confidence: float = 1.0

class LLMIntentClassifier:
    def __init__(self, client: LLMClient, fallback=None, cache=None) -> None: ...
    def classify(self, query: str) -> Intent: ...            # IntentClassifier port
    def analyze(self, query: str) -> IntentResult: ...        # richer: symbol/entities/filters
```

## Prompt & output schema

System prompt: "Classify a code-search query. Return ONLY JSON:
`{"intent":"LOCATE|EXPLAIN|FILTER","symbol":str|null,"entities":[str],"filters":{"type":str?,"lang":str?,"path_glob":str?,"since":str?},"confidence":0..1}`."
Three few-shot examples mirroring the worked examples below.

## FILTER → SQL (safe)

`filters` keys are **allowlisted** and mapped to parameterized predicates over `nodes` / `file` nodes:

```python
ALLOWED = {"type": "n.type = :type",
           "lang": "json_extract(n.attrs,'$.lang') = :lang",
           "path_glob": "n.uid GLOB :path_glob",
           "since": "json_extract(f.attrs,'$.mtime') >= :since"}
# build: SELECT ... FROM nodes n [JOIN file f ...] WHERE <AND of allowed predicates>  -- all values bound
```
No string interpolation of values, ever. Unknown keys are dropped (logged). Optionally rerank the FILTER
result set by vector similarity to the raw query.

## Algorithm / step-by-step

1. Cache lookup by `hash(query)`; hit → return.
2. `text = client.complete(system, query)`; parse JSON; validate against the schema (types, enum, range).
3. On parse/validation error or exception → `fallback.classify(query)` (regex `DefaultIntentClassifier`).
4. If `intent == LOCATE` and `symbol` is set, verify it exists in `nodes` (else downgrade to EXPLAIN — guards hallucinated symbols).
5. If `confidence < 0.5` → force `EXPLAIN` (safe richer path).
6. Cache and return.

**Worked examples:**
- `"where is DeviceNotificationService used?"` → `{intent:LOCATE, symbol:"DeviceNotificationService", confidence:0.96}`.
- `"how do mass notifications work?"` → `{intent:EXPLAIN, entities:["mass notification"], confidence:0.9}`.
- `"show me Go functions in pkg/queue changed since 2026-06-15"` →
  `{intent:FILTER, filters:{type:"function", lang:"go", path_glob:"pkg/queue/*", since:"2026-06-15"}}` →
  SQL `... WHERE n.type=:type AND json_extract(n.attrs,'$.lang')=:lang AND n.uid GLOB :path_glob AND json_extract(f.attrs,'$.mtime')>=:since`.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/planner.py` | **Modify** — `LLMIntentClassifier`, real `_filter()` using the safe SQL builder; `RetrievalPlanner` consumes `IntentResult` |
| `src/memorydb/ports.py` | **Modify** — add the `LLMClient` Protocol |
| `src/memorydb/filters.py` | **New** — allowlisted FILTER→SQL builder |

## Edge cases & failure modes

- **Invalid/empty JSON, timeout, exception:** fall back to regex (never raise to the caller).
- **Hallucinated symbol** not in DB: downgrade LOCATE→EXPLAIN.
- **Injection attempt** in a filter value (`'; DROP TABLE nodes;--`): value is bound, never interpolated → inert.
- **Multi-intent query:** take the model's top intent; ambiguity (low confidence) → EXPLAIN.
- **Empty FILTER result:** return empty cleanly (the API surfaces "no matches").

## Test plan

Zero-dep with a `FakeLLM(canned_json)`:

- `test_routes_locate/explain/filter` — canned JSON → correct `Intent` and fields.
- `test_filter_sql_is_parameterized` — assert the built SQL has placeholders and a params dict (no literals).
- `test_injection_neutralized` — malicious filter value → bound param, query runs, table intact.
- `test_fallback_on_llm_error` — `FakeLLM` raises → regex classifier result.
- `test_hallucinated_symbol_downgraded` — LOCATE on a non-existent symbol → EXPLAIN.

## Performance & scale

One small LLM call per *uncached* query (cache keyed by query hash); FILTER SQL uses existing indexes. The
LLM call is the latency cost — bounded by caching and the tiny prompt.

## Tasks

- [ ] `LLMClient` port + `IntentResult` schema + validation
- [ ] `LLMIntentClassifier.analyze/classify` with cache + fallback chain
- [ ] allowlisted FILTER→SQL builder (parameterized) + planner `_filter()`
- [ ] symbol-existence guard + low-confidence→EXPLAIN
- [ ] zero-dep tests (routing / parameterization / injection / fallback / hallucination)

## Open questions

- **Structured output**: rely on JSON-in-text + validation, or a tool/function-calling schema if the client
  supports it? **Lean** JSON+validation for portability; use tool-calling when the injected client offers it.
- **Entity → concept linkage**: should `entities` seed the concept layer ([concept-ontology-layer.md](concept-ontology-layer.md))? **Lean** yes, once concepts exist.

## Risks

- **Provider lock-in** if we hardcode prompts to one model → keep the prompt generic; client injected (TD-002).
- **Misrouting** hurting UX → default ambiguous to EXPLAIN and always keep the regex fallback.

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
- [context-builder-packing.md](context-builder-packing.md), [hybrid-ranker.md](hybrid-ranker.md)
