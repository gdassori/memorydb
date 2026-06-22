---
title: "Python precise resolver — high-confidence edges via ast + symtable"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-005, TD-002]
components: [adapters/code]
---

# Python precise resolver (ast + symtable)

> A Python-specific `Extractor` that emits **high-confidence (0.95–1.0) edges** using the stdlib
> `ast` and `symtable` modules, upgrading the coarse name-based edges from the tree-sitter
> [CodeAdapter](code-adapter-treesitter.md) ([TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md)).
> No third-party dependency — it stays in the zero-dep spirit of the core.

## Goal

For Python files, resolve def/class/import/call/inheritance with scope awareness and emit edges that
**supersede** the coarse tree-sitter edges for the same `(src, dst, relation)`. Done = after running
the precise resolver, `LOCATE` on a Python symbol returns callers with confidence ≥ 0.95 and no
name-collision false positives within resolvable scope.

## Background & constraints

`ast` gives the syntax tree; `symtable` gives **scope/binding** info (which names are locals, globals,
free vars, imports) — together they resolve most intra/inter-module references without running code.
Shares the `uid` (FQN) scheme with the tree-sitter adapter so edges merge. Kept dependency-free on
purpose; `jedi`/`pyright`/LSP are considered in Open Questions.

## Approach

Two phases over the indexed Python file set: (1) build a global **definition table** `uid -> def` and a
per-module **import map**; (2) walk each module's `ast`, and for every `Call`/`ClassDef.bases`/`Import*`
resolve the target name through `symtable` scopes → import map → global def table, emitting an `Edge`
at a confidence reflecting resolution certainty.

## Data model & interfaces

```python
class PythonResolver:                    # implements the Extractor port (TD-002)
    def __init__(self, repo_root: str = ".") -> None: ...
    def load_module(self, path: str) -> "ModuleIR": ...        # ast + symtable + import map
    def extract(self, path: str) -> tuple[list[Node], list[Edge]]: ...   # single-file (uses cached global table)
    def resolve_repo(self, paths: list[str]) -> tuple[list[Node], list[Edge]]:  # cross-module

@dataclass
class ModuleIR:
    module: str                  # dotted module name from repo-relative path
    tree: "ast.AST"
    symtab: "symtable.SymbolTable"
    imports: dict[str, str]      # local alias -> fully-qualified target
```

**uid scheme:** identical to [code-adapter-treesitter.md](code-adapter-treesitter.md) —
`relpath::qualname`. **Supersession:** edges are upserted with `Store.upsert_edge(src, dst, rel,
confidence=...)`; since the table has `UNIQUE(src, dst, relation)`, a precise edge (0.95–1.0)
overwrites the coarse one (≤0.9) on conflict — higher confidence wins.

## Algorithm / step-by-step

1. **Module name:** repo-relative path → dotted module (`pkg/sub/x.py` → `pkg.sub.x`; `__init__.py` →
   the package).
2. **Imports:** walk `ast.Import` / `ast.ImportFrom` (incl. relative `.`/`..`, aliases, `from m import *`
   → low confidence) → `imports` map.
3. **Defs:** `FunctionDef`/`AsyncFunctionDef`/`ClassDef` → `Node`s with qualname from the nesting stack.
4. **symtable scopes:** for each function/class, get its `SymbolTable` to know whether a referenced name
   is local, a parameter, global, free, or imported.
5. **Calls:** for `ast.Call`, resolve the callee:
   - direct name bound to a local/global def in the table → **1.0**
   - name via the import map to a known module symbol → **0.97**
   - attribute on a known module alias (`mod.func`) → **0.95**
   - `from m import *` candidate → **0.5**
   - attribute on an untyped receiver (`self.x()` where `x` is resolvable via the class MRO) → **0.9**;
     otherwise **skip** (do not guess).
6. **Inheritance:** `ClassDef.bases` resolved like calls → `INHERITS` edges.
7. Emit nodes + edges; the Indexer upserts (supersedes coarse edges).

**Worked example:**
```python
# app/jobs.py
from services.notifications import NotificationService
def run():
    NotificationService().send("hi")     # CALLS services/notifications.py::NotificationService.send @0.97
```

## What changes

| File | Change |
|------|--------|
| `src/memorydb/adapters/code/python_resolver.py` | **New** — `PythonResolver`, `ModuleIR` |
| `src/memorydb/adapters/code/registry.py` | **Modify** — register `PythonResolver` as the precise extractor for `.py`, run after the coarse pass |

## Edge cases & failure modes

- **Conditional / try-except imports:** record all branches at reduced confidence (0.7).
- **Re-exports / `__all__`:** follow re-exports through `__init__` where statically determinable.
- **Relative imports beyond top package:** resolve against `repo_root`; unresolved → skip + log.
- **Properties / descriptors / metaclasses:** treat as methods; dynamic behavior unresolved → skip.
- **Closures / nested funcs:** qualname includes the enclosing function (`outer.<locals>.inner`).
- **Monkeypatching / `setattr`:** inherently unresolvable → skip (never guess; that's the coarse path's job).

## Test plan

Fully **zero-dep** (ast + symtable are stdlib):

- `test_resolves_direct_call` — module with a local def + call → edge @1.0.
- `test_resolves_imported_call` — cross-module import → edge @0.97 with correct dst uid.
- `test_star_import_low_confidence` — `from m import *` → @0.5.
- `test_precise_supersedes_coarse` — upsert a coarse @0.6 edge, run resolver, assert it becomes ≥0.95.
- `test_unresolvable_skipped` — `obj.method()` unknown receiver → no edge (no false positive).

## Performance & scale

`compile`/`ast.parse` is fast; `symtable` is cheap. Two passes over the Python file set; memory bound by
the global def table (one row per symbol). Comfortable to tens of thousands of Python files.

## Tasks

- [ ] module-name + import-map resolution (relative, aliased, star, package `__init__`)
- [ ] def/class node extraction with nesting-aware qualname (shared uid scheme)
- [ ] symtable-driven scope resolution for calls + bases
- [ ] confidence tiers + supersession via upsert
- [ ] zero-dep test suite incl. the supersedes-coarse case

## Open questions

- Use `jedi`/`pyright` for even better attribute/type resolution? **Lean no for v1** (adds a heavy dep);
  revisit if `self.x()`/attribute resolution recall is insufficient. Could be a separate higher-tier extractor.
- Whole-repo resolution vs per-file incremental: cross-module calls need the global table. **Lean** build
  the global def table during the Indexer's pass 1, then resolve per file in pass 2.

## Risks

- **Stdlib-only ceiling:** attribute/type inference is limited without a type engine → accept skips over
  guesses; coarse edges still cover the gap at low confidence.
- **uid drift** vs the tree-sitter adapter would break supersession → enforce one shared uid function in code.

## References

- [TD-005](../../decisions/TD-005-multilang-treesitter-coarse-edges-confidence.md), [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md)
- [code-adapter-treesitter.md](code-adapter-treesitter.md), [indexer-ingestion-pipeline.md](indexer-ingestion-pipeline.md)
- Python stdlib `ast`, `symtable`.
