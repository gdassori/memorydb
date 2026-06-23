# Sample eval suite

A tiny 2-file Python fixture with a deterministic call graph, for the retrieval-quality harness
([eval-harness spec](../../../docs/specs/active/eval-harness.md)).

- `repo/notifications.py` — `send_notification` (calls `enqueue`), `enqueue`.
- `repo/jobs.py` — `MassNotificationJob.run` (calls `send_notification`, imported cross-file).
- `cases.jsonl` — labeled LOCATE/EXPLAIN cases with ground-truth `expected_uids`.

Run:

```bash
memorydb-eval run eval/suites/sample
```

LOCATE is exact (the call graph is known). With the default extractors, the `send_notification`
cross-file caller is resolved **precisely** by the [PythonResolver](../../../src/memorydb/adapters/code/python_resolver.py)
(ast/symtable) at confidence 0.97, so `precision@≥0.9` is 1.0. With only the coarse tree-sitter
`CodeAdapter` it would be a by-name 0.6 edge (counted in `precision` but not `precision@≥0.9`) — the
gap that `precision@≥0.9` is designed to expose (TD-005).
