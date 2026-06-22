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

LOCATE is exact (the call graph is known). The `send_notification` LOCATE case has a *cross-file*
caller resolved by name → confidence 0.6, so it counts toward `precision` but not `precision@≥0.9`
(the coarse-edge column, TD-005). The `enqueue` case is a same-file call → confidence 0.9.
