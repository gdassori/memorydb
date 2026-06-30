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
- **Entity → concept linkage**: should `entities` seed the concept layer ([concept-ontology-layer.md](../active/concept-ontology-layer.md))? **Lean** yes, once concepts exist.

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

## Review remediation (2026-06-25 — PR #4 mega review)

An adversarial multi-agent review (27 raised → 24 confirmed / 1 refuted) found the headline SQL-injection claim
holds (every value is bound and inert) but surfaced real correctness/robustness defects, now all fixed +
regression-tested (`test_p4_*`):

- **P4-1 (High):** a non-scalar FILTER value (an LLM can return `{"lang":["go","py"]}`) hit `sqlite3.execute`
  and raised `ProgrammingError` out of `MemoryDB.ask` — breaking *never raise to the caller*. `build_filter_query`
  now drops any non-`str/int/float` value (like an unknown key); `planner._filter` also wraps the execute and
  degrades to the clean empty result on any DB error.
- **P4-2 (Medium):** a bare-year/numeric `since` string (`"2026"`) was read by `float()` as epoch `2026.0` (~1970),
  silently widening recency to *everything*. `_to_epoch` now treats a **numeric type** as an epoch and a **string**
  as an ISO date only (bare year / `1e9` / 10-digit epoch strings are rejected → dropped); non-finite values dropped.
- **P4-3 (Medium):** `since` used an INNER JOIN, so a symbol whose file had no stored `mtime` (indexer `OSError`)
  or no `file_uid` silently vanished. Now a LEFT JOIN with the recency predicate deciding membership — `since`
  returns only confirmed-recent symbols (unknown recency is **excluded by design**: a recency filter cannot vouch
  for an unknown mtime), but the exclusion is explicit, not a join artifact.
- **P4-4 (Medium):** `analyze()` cached the post-guard verdict, so a symbol indexed after a hallucination downgrade
  kept returning stale EXPLAIN. Now only the store-independent half (LLM parse + confidence) is cached; the
  symbol-existence downgrade runs **fresh** every call.
- **P4-5 (Medium):** the planner mutated the injected classifier (`symbol_exists = self._symbol_exists`), so one
  classifier shared by two planners checked the *first* planner's store. The planner no longer mutates the
  classifier — it applies the hallucination guard directly against its own store in `retrieve()`.
- **P4-6 (Low):** `path_glob` matched `n.uid` (which carries `::qualname`), so file-anchored globs (`*.py`,
  `pkg/queue/*.py`) matched nothing. Now matches `n.file_uid` (the owning file path).
- **P4-7 (Low):** a lowercase/mixed-case `intent` (`"locate"`) failed enum validation and discarded the whole
  verdict to the regex fallback. The intent is now upper-cased before validation.
- **Also:** the symbol-guard exception is swallowed (never raises); the default query cache is bounded
  (oldest-evicted, `max_cache=4096`); `IntentResult` is `frozen` (a cached result can't be mutated); FILTER
  respects the caller's `k`; standalone-classifier (no `symbol_exists`) hallucination caveat documented.

Refuted: `locate()` grounding onto a file node while `_symbol_exists` excludes them — benign (LLM symbols are
code identifiers, not file names).

### Second round — re-review of the P4 fixes (2026-06-25)

A re-review of the remediation (8 raised → 5 confirmed / 2 refuted) caught regressions the P4 fixes introduced —
the codebase's recurring "every fix adds a regression" pattern. All fixed + regression-tested (`test_p4r_*`):

- **P4R-1 (High):** the P4-2 finiteness guard called `math.isfinite(value)` on the *raw* `since` value, so a
  huge-int (`10**400`, an LLM JSON literal) raised `OverflowError` straight out of `MemoryDB.ask` — re-introducing
  the exact "never raise" violation P4-1 fixed (it escaped because `build_filter_query` was called outside
  `_filter`'s try). Now `_to_epoch` converts to `float` inside `try/except (OverflowError, ValueError)` (drop on
  overflow), and `_filter` wraps the builder call too.
- **P4R-2 (Medium):** the P4-2 `datetime.fromisoformat` parse was interpreter-dependent — Z-suffix / basic-format /
  week-date `since` strings parse on 3.11+ but raise on 3.10, giving different FILTER result sets per Python version
  (CI runs 3.10/3.11/3.12). `_to_epoch` now parses with an explicit `strptime` format set (`_SINCE_FORMATS`) that
  behaves identically on every interpreter, normalizing a trailing `Z` to `+00:00` first.
- **P4R-3 (Low):** `frozen=True` blocks attribute reassignment but not mutation of the contained `filters` dict, so
  a caller doing `out["filters"].clear()` corrupted the cached `IntentResult`. `_filter` now returns `dict(result.filters)`.
- **P4R-4 (Low):** `limit=k` silently capped a "list-all" FILTER at `k` (default 5). `_filter` now fetches `k+1`,
  sets a `truncated` flag, and slices to `k` — the cap is signalled, not silent.

Refuted: planner-side `_symbol_exists` raising out of `retrieve()` (a trivial store lookup; "never raise" is the
LLM-fallback contract, not DB errors); `max_cache=0` disabling the bound (a deliberate opt-out, default is 4096).

## References

- [TD-007](../../decisions/TD-007-intent-routed-retrieval-tj-is-orchestration.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
- [context-builder-packing.md](context-builder-packing.md), [hybrid-ranker.md](hybrid-ranker.md)
