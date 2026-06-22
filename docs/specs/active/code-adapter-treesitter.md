---
title: "CodeAdapter — multilang symbol & coarse-edge extraction via tree-sitter"
status: active
created: 2026-06-22
author: claude
related_tds: [TD-005, TD-002, TD-006]
components: [adapters/code]
---

# CodeAdapter — tree-sitter extraction

> The `CodeAdapter` turns source files in many languages into substrate `Node`s (symbols) and
> `Edge`s (relations), implementing the `Extractor` port ([TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)).
> It produces **coarse, name-based edges tagged with `confidence < 1.0`** ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md));
> precise per-language resolvers ([python-precise-resolver.md](python-precise-resolver.md)) upgrade them later.

## Goal

`CodeAdapter().extract(path)` returns `(nodes, edges)` for one source file across the supported
languages, with stable `uid`s, populated `attrs` (signature/docstring/span), and CALLS/IMPORTS/
INHERITS edges whose `confidence` reflects how the call name was resolved. Done = the `Indexer`
([indexer-ingestion-pipeline.md](indexer-ingestion-pipeline.md)) can index a mixed-language repo
and `LOCATE`/`EXPLAIN` work over it.

## Background & constraints

tree-sitter gives a CST, **not** symbol resolution: a call node is just text. So this adapter
extracts *nodes* precisely (the grammar knows what a function is) and *edges* heuristically by
name, recording uncertainty in `confidence` ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)).
Lives in the optional `[code]` extra (`tree-sitter` + `tree-sitter-language-pack`); the core stays
zero-dep ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)).

## Approach

A `LanguageRegistry` maps file extension → a `LanguageSpec` (grammar name + tree-sitter queries).
`CodeAdapter.extract` parses the file once, runs per-language `.scm` queries to capture symbol and
reference nodes, builds `Node`s, then resolves each reference name against the file's local symbol
table and import set to emit `Edge`s with a confidence drawn from the resolution tier.

## Data model & interfaces

```python
from dataclasses import dataclass
from memorydb.models import Node, Edge, Rel

@dataclass
class LanguageSpec:
    grammar: str                 # tree-sitter-language-pack name, e.g. "python"
    extensions: tuple[str, ...]  # (".py",)
    symbols_query: str           # .scm capturing @function/@class/@method/@import
    refs_query: str              # .scm capturing @call/@base/@import.target

class LanguageRegistry:
    def spec_for(self, path: str) -> LanguageSpec | None: ...   # by extension; None => skip

class CodeAdapter:                      # implements the Extractor port (TD-002)
    def __init__(self, registry: LanguageRegistry | None = None, repo_root: str = ".") -> None: ...
    def extract(self, path: str) -> tuple[list[Node], list[Edge]]: ...
```

**uid scheme (shared contract).** `uid = f"{relpath}::{qualname}"`, e.g.
`services/notifications.py::NotificationService.send`. `relpath` is POSIX, relative to `repo_root`;
`qualname` is the dotted nesting path. A `file`-type node uses `uid = relpath`. Collisions (two defs,
same qualname in one file — e.g. conditional defs) get a `#N` suffix in definition order. This scheme
is shared verbatim with [python-precise-resolver.md](python-precise-resolver.md) so edges merge.

**Node `attrs`:** `{lang, signature, docstring, start_byte, end_byte, start_line, end_line}`.

## Algorithm / step-by-step

1. `spec = registry.spec_for(path)`; if `None`, return `([], [])` (unsupported → skip, logged).
2. Parse bytes → tree (lazy-load the grammar; cache per grammar).
3. Run `symbols_query`; for each capture build a `Node` (uid per scheme, `type` ∈ {function, class,
   method, import}, `body` = source slice, `attrs` filled). Maintain a **local symbol table**
   `name -> uid` and an **import map** `alias -> module/symbol`.
4. Run `refs_query`; for each reference resolve the callee/base/import name through the **tier ladder**:
   - same-file symbol table → `confidence = 0.9`
   - import-scoped (name came via a tracked import) → `0.6`
   - bare global name match against the index later (deferred to the Indexer's pass 2) → `0.3`
   - unresolved (e.g. `obj.method()` with unknown receiver) → emit at `0.2` or skip (config).
5. Emit `Edge(src=enclosing_symbol_uid, dst=resolved_uid, relation=CALLS|INHERITS|IMPORTS,
   confidence=tier, source="treesitter")`.

**Worked example** (`services/notifications.py`):
```python
import redis
class NotificationService:
    def send(self, user, msg):           # -> Node uid services/notifications.py::NotificationService.send
        redis.Queue().push(msg)          # CALLS push (import-scoped via `redis`) conf 0.6
```
→ nodes: the class, the method, the `redis` import; edges: `…::NotificationService.send --CALLS--> redis::Queue.push` @0.6.

## What changes

| File | Change |
|------|--------|
| `src/memorydb/adapters/code/__init__.py` | **Modify** — replace stub `CodeAdapter` with the real implementation |
| `src/memorydb/adapters/code/registry.py` | **New** — `LanguageRegistry`, `LanguageSpec` |
| `src/memorydb/adapters/code/queries/` | **New** — `.scm` query files per language |
| `pyproject.toml` | **Modify** — `[code]` extra already declares `tree-sitter`, `tree-sitter-language-pack` |

## Edge cases & failure modes

- **Parse errors / ERROR nodes:** extract what parsed; count error regions in `attrs`; never raise.
- **Unsupported language:** `spec_for` returns `None` → skip + log (don't fail the index).
- **Anonymous/lambda functions:** synthesize uid `…::<lambda@line>`; type `function`.
- **Overloads / duplicate names:** `#N` suffix; calls resolve to all candidates at reduced confidence.
- **Vendored/generated code:** out of scope here — the Indexer's ignore rules exclude it.

## Test plan

- **[code] extra:** parse fixtures in python/go/js; assert node counts, types, and edge confidences.
- **Zero-dep:** unit-test the resolution-tier function against a hand-built `(symbol_table, import_map,
  ref_name)` table → asserts the right confidence without parsing anything.

## Performance & scale

Parsing is O(file size); tree-sitter is fast (incremental-capable). One parse per file; grammars cached.
Bottleneck at repo scale is I/O + embedding, not parsing. Coarse edges are cheap; precise upgrades are
opt-in per language.

## Tasks

- [x] `LanguageRegistry` + `LangSpec` + extension map (python, javascript, typescript, go, rust)
- [x] `CodeAdapter.extract` → `Extraction(nodes, edges, pending)`: parse → field-based tree walk → local symbol table + import set → resolution-tier edges (0.9 in-file / 0.6 import-scoped pending / 0.3 bare-name pending)
- [x] uid scheme `relpath::qualname` + deterministic `#startbyte` collision suffix (shared with the precise resolver)
- [x] attrs (lang, signature, docstring, start/end line, `file_uid`) — docstring handles the bare-`string` and `expression_statement`-wrapped grammars
- [x] [code]-extra tests: registry, python nodes/edges/inheritance/pending, unsupported-skip, JS smoke — 6 green
- [ ] add java / c / cpp specs; richer call-receiver typing; optional `.scm` query path
- [ ] zero-dep unit test of the resolution-tier function in isolation (currently exercised via the adapter, which needs the extra)

> **Impl notes (2026-06-22):** used a **field-based manual tree walk** (stable across tree-sitter
> versions) instead of `.scm` queries; built parsers via `tree_sitter.Parser(get_language(name))`
> because `tree_sitter_language_pack.get_parser` ships a broken parser binding (its `parse` rejects
> `bytes`). Returns an `Extraction` (not a bare tuple) so the indexer gets the **pending** edges (C2).

## Open questions

- Receiver typing for `obj.method()`: skip vs emit @0.2? **Lean emit @0.2** so EXPLAIN has a thread, but
  never surface @0.2 edges in LOCATE.
- Query maintenance: hand-write `.scm` vs reuse the language-pack's bundled tags queries? **Lean reuse**
  where they exist, override where they miss methods/imports.

## Risks

- **Edge false positives** from name collisions across files → mitigate with confidence tiers + precise
  resolvers + never treating <0.9 edges as exact in LOCATE ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)).
- **Grammar/version drift** in tree-sitter-language-pack → pin the version in the `[code]` extra.

## Review remediation (2026-06-22)

- **Unresolved (global-name) edges (C2):** `extract()` cannot emit an `Edge` to a name it can't resolve in-file,
  because `Edge.dst` is a uid and `Store.upsert_edge` raises on a missing endpoint (verified). So the **0.3
  global-name tier is NOT returned as an `Edge`**; the adapter returns it as a **pending edge**
  `(src_uid, dst_name, relation, confidence)` that the [indexer](indexer-ingestion-pipeline.md) resolves against the
  global symbol table in pass 2 (kept-low or dropped if still unresolved).
- **Stable uids for overloads:** assign the `#N` disambiguation suffix by a **deterministic key** (start byte
  offset), not parse iteration order, so a re-parse yields the same uid and the indexer doesn't churn/duplicate.
- **file linkage:** stamp `attrs.file_uid` (and the file's `mtime`/`lang`) on every symbol so FILTER and the ranker
  have a standard join key (C5).

## References

- [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md), [TD-006](../../decisions/TD-006-graph-aware-embeddings-staleness.md)
- [python-precise-resolver.md](python-precise-resolver.md), [indexer-ingestion-pipeline.md](indexer-ingestion-pipeline.md)
- tree-sitter-language-pack (precompiled grammars), tree-sitter query (`.scm`) syntax.
