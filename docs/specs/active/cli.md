---
title: "Command-line interface (memorydb)"
status: planned
created: 2026-06-22
author: claude
related_tds: [TD-002, TD-004]
components: [cli, api]
---

# CLI — `memorydb`

> A thin stdlib `argparse` CLI over the [`MemoryDB` facade](public-api-facade.md): index a repo, query it,
> inspect status. Zero extra dependencies ([TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md));
> the CLI is orchestration, never new logic.

## Goal

`memorydb index <path>`, `memorydb query "<q>"`, `memorydb status`, `memorydb locate <symbol>` work end to end
against a `--db <file>`. Done = a user indexes a repo and asks questions from the shell without writing Python.

## Background & constraints

Mirror the thin-orchestrator philosophy (a CLI that composes, prints what it does). stdlib `argparse` only —
no `click`/`typer` (keeps zero-dep). All real work lives in the facade.

## Data model & interfaces

```
memorydb --db PATH <command> [opts]

  index PATH [--include GLOB ...] [--exclude GLOB ...] [--no-embed]
  query "TEXT" [-k N] [--depth N] [--context] [--budget N] [--json]
  locate SYMBOL [--json]
  explain "TEXT" [--depth N] [--json]
  status                       # node/edge/embedding counts, schema version, dirty count
  reembed                      # refresh stale embeddings
```

Entry point declared in `pyproject.toml`:
```toml
[project.scripts]
memorydb = "memorydb.cli:main"
```

## Algorithm / step-by-step

1. Parse args (`argparse` subparsers); resolve `--db` (default `./memorydb.sqlite`).
2. `MemoryDB.open(db, embedder=<resolved>)` — embedder from `--embedder` or the default (warn if default).
3. Dispatch to the facade method; render output as human text (default) or `--json`.
4. Exit codes: 0 ok, 1 usage error, 2 runtime error (with a clear message).

**Worked example:**
```
$ memorydb --db orbital.db index ~/src/orbital
indexed 412 files · ~11k symbols · 3.2k edges (41 unresolved) · embedded 11k nodes
$ memorydb --db orbital.db query "where is send_notification used?"
LOCATE send_notification
  MassNotificationJob  CALLS  (conf 0.97)  app/jobs.py:88
```

## What changes

| File | Change |
|------|--------|
| `src/memorydb/cli.py` | **New** — `argparse` subcommands + renderers + `main()` |
| `pyproject.toml` | **Modify** — `[project.scripts] memorydb = "memorydb.cli:main"` |

## Edge cases & failure modes

- **DB does not exist for `query`:** create-or-open is fine for `index`; for `query` on an empty DB → "no data, run index".
- **Default embedder used:** print a one-line warning (`HashingEmbedder` is not semantic-quality).
- **Bad glob / path:** usage error (exit 1) with the offending value.
- **Large output:** `query` truncates to terminal-friendly size unless `--json`.
- **Ctrl-C mid-index:** the indexer is resumable (hashes) → re-running continues.

## Test plan

Zero-dep (invoke `main([...])` in-process with a temp DB + fake extractor):

- `test_index_then_query` — `index` a fixture dir, `query` returns expected LOCATE rows.
- `test_status` — counts match what was inserted; schema version shown.
- `test_json_output` — `--json` emits valid parseable JSON.
- `test_usage_errors` — bad args → exit code 1, message on stderr.

## Performance & scale

CLI overhead is negligible; cost is the underlying index/query. `status` is O(1) count queries.

## Tasks

- [ ] `argparse` subparsers (index/query/locate/explain/status/reembed)
- [ ] human + `--json` renderers (LOCATE rows, EXPLAIN/context, status)
- [ ] `[project.scripts]` entry point
- [ ] embedder resolution + default warning
- [ ] zero-dep in-process tests

## Open questions

- **Interactive REPL** (`memorydb shell`)? **Lean** defer; one-shot commands first.
- **Config file** (`.memorydb.toml`) for include/exclude/embedder? **Lean** add once flags get unwieldy.

## Risks

- **Putting logic in the CLI** instead of the facade → keep `cli.py` to parsing + rendering only.

## References

- [TD-002](../../decisions/TD-002-ports-and-adapters-generic-substrate.md), [TD-004](../../decisions/TD-004-zero-dep-core-bruteforce-vectors.md)
- [public-api-facade.md](public-api-facade.md), [indexer-ingestion-pipeline.md](indexer-ingestion-pipeline.md)
