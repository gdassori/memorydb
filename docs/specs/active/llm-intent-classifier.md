---
title: "LLM intent classifier & the FILTER path"
status: completed
created: 2026-06-22
completed: 2026-06-25
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

- [x] `LLMClient` port + `IntentResult` schema + validation
- [x] `LLMIntentClassifier.analyze/classify` with cache + fallback chain
- [x] allowlisted FILTER→SQL builder (parameterized) + planner `_filter()`
- [x] symbol-existence guard + low-confidence→EXPLAIN
- [x] zero-dep tests (routing / parameterization / injection / fallback / hallucination)

## Implementation notes (2026-06-25)

- **Pydantic, not dataclass.** `IntentResult` is a `pydantic.BaseModel` (TD-004); `confidence` is validated
  to `[0, 1]` via `Field(ge=0, le=1)`, so an out-of-range model reply is a parse failure → regex fallback.
- **`LLMClient` port** added to `ports.py` (`complete(system, user) -> str`); no provider imported (TD-002).
- **Fallback chain.** `LLMIntentClassifier._analyze_uncached` wraps the call in a broad `except`: timeout,
  empty/invalid JSON (`_extract_json` tolerates a ```` ```json ```` fence + surrounding prose), or schema/range
  violation all return `IntentResult(intent=fallback.classify(query))`. It never raises to the caller.
- **Symbol guard lives in the planner, injected as a callback.** The spec lists the hallucinated-symbol
  downgrade as a classifier step, but verifying existence needs the store. To keep the classifier store-free
  (TD-002) it takes a `symbol_exists: Callable[[str], bool]`; `RetrievalPlanner.__init__` auto-wires it to a
  `nodes` lookup (name-or-uid, file nodes excluded) for any analyze-capable classifier that doesn't set one.
  `analyze()` applies low-confidence→EXPLAIN and the symbol downgrade; results are cached by query string.
- **LOCATE uid (C4).** An LLM-supplied `symbol` is tried as the *first* `locate()` candidate, so a uid resolves
  to exactly one target and the ambiguity grouping collapses.
- **mtime is epoch, not ISO (supersedes the C5 remediation).** The shipped indexer stamps `attrs.mtime` as an
  epoch number (`os.path.getmtime`), so `filters.build_filter_query` coerces a `since` date/datetime to a float
  epoch (UTC) and binds it — a numeric comparison against the stored value, **no re-index**. The value stays
  bound (injection-safe). `since` uses an explicit `JOIN nodes f ON f.uid = n.file_uid AND f.type='file'`.
- **FILTER builder** (`filters.py`) iterates a fixed allowlist (deterministic SQL/params), drops unknown/empty
  keys (returned for logging), excludes file nodes, and orders by `uid`. The planner re-sorts the fetched nodes
  by uid (`get_nodes` is unordered). Vector reranking of the FILTER set is deferred (deterministic order for v1).

## Open questions

- **Structured output**: rely on JSON-in-text + validation, or a tool/function-calling schema if the client
  supports it? **Lean** JSON+validation for portability; use tool-calling when the injected client offers it.
- **Entity → concept linkage**: should `entities` seed the concept layer ([concept-ontology-layer.md](concept-ontology-layer.md))? **Lean** yes, once concepts exist.

## Risks

- **Provider lock-in** if we hardcode prompts to one model → keep the prompt generic; client injected (TD-002).
- **Misrouting** hurting UX → default ambiguous to EXPLAIN and always keep the regex fallback.

## Review remediation (2026-06-22)

- **FILTER joins (C5):** there is no implicit `JOIN file f`. The `since`/`lang` predicates use the symbol's
  `attrs.file_uid` to reach the owning `file` node (or read the denormalized `attrs.mtime`/`attrs.lang` stamped by the
  indexer). Define the join explicitly in the builder.
- **mtime format:** store mtime as an **ISO-8601 UTC string** so `json_extract(...,'$.mtime') >= :since` is a correct
  lexical comparison; epoch *numbers* would mis-compare against a text bind via type affinity. Values remain bound
  (injection-safe).
- **LOCATE uid (C4):** when the classifier returns a `symbol`, prefer resolving it to a **uid** and pass that to
  `references_to`, so the planner's ambiguity grouping collapses to a single target.

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
- [context-builder-packing.md](context-builder-packing.md), [hybrid-ranker.md](hybrid-ranker.md)
