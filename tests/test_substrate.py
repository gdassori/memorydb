"""v0 substrate tests (TD-007 routing, TD-006 staleness, recursive-CTE traversal).

Runs with core deps only (pydantic, no optional extras): `python tests/test_substrate.py` or `pytest`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import HashingEmbedder, Node, Rel, RetrievalPlanner, Store  # noqa: E402
from memorydb import query as Q  # noqa: E402

# A tiny notification subsystem: MassNotificationJob -> send_notification -> {RedisQueue, PushProvider, NotificationLog}
_NODES = {
    "MassNotificationJob": "Job that triggers mass notifications to many users",
    "send_notification": "Send a single notification to a user via the queue",
    "RedisQueue": "Redis backed queue for notification messages",
    "PushProvider": "Push notification provider gateway firebase",
    "NotificationLog": "Persistent log of sent notification records",
}
_EDGES = [
    ("MassNotificationJob", "send_notification", Rel.CALLS),
    ("send_notification", "RedisQueue", Rel.CALLS),
    ("send_notification", "PushProvider", Rel.CALLS),
    ("send_notification", "NotificationLog", Rel.WRITES),
]


def build():
    store = Store(":memory:")
    for uid, body in _NODES.items():
        store.upsert_node(Node(uid=uid, type="function", name=uid, body=body))
    for src, dst, rel in _EDGES:
        store.upsert_edge(src, dst, rel)
    emb = HashingEmbedder(dim=64)
    for uid, body in _NODES.items():
        store.set_embedding(store.id_for(uid), emb.embed([body])[0], model="hashing")
    return store, emb


def test_locate_is_exact_graph():
    store, emb = build()
    res = RetrievalPlanner(store, emb).retrieve("where is send_notification used?")
    assert res["intent"] == "LOCATE"
    assert res["symbol"] == "send_notification"
    assert "MassNotificationJob" in {r["src_uid"] for r in res["references"]}


def test_explain_seeds_and_expands():
    store, emb = build()
    res = RetrievalPlanner(store, emb).retrieve("how does the notification queue work")
    assert res["intent"] == "EXPLAIN"
    assert res["seeds"], "vector search should find entry points"
    uids = {n["uid"] for n in res["nodes"]}
    assert "send_notification" in uids
    assert len(res["edges"]) >= 1


def test_traverse_respects_depth():
    store, _ = build()
    seed = store.id_for("MassNotificationJob")
    d1 = {r["id"] for r in Q.traverse(store, [seed], max_depth=1, direction="out")}
    assert store.id_for("send_notification") in d1
    assert store.id_for("RedisQueue") not in d1  # 2 hops away
    d2 = {r["id"] for r in Q.traverse(store, [seed], max_depth=2, direction="out")}
    assert store.id_for("RedisQueue") in d2


def test_relation_filter():
    store, _ = build()
    seed = store.id_for("send_notification")
    writes = Q.traverse(store, [seed], max_depth=1, relations=[Rel.WRITES], direction="out")
    reached = {r["id"] for r in writes if r["depth"] == 1}
    assert reached == {store.id_for("NotificationLog")}


def test_embedding_staleness():
    store, _ = build()
    assert store.dirty_nodes() == []  # everything embedded -> clean
    store.upsert_edge("MassNotificationJob", "RedisQueue", Rel.USES)  # new edge
    dirty = {n["uid"] for n in store.dirty_nodes()}
    assert {"MassNotificationJob", "RedisQueue"} <= dirty


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        fn()
        print(f"ok  {name}")
    print(f"\nall green ({len(tests)} tests)")
