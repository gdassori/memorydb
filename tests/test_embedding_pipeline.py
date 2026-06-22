"""Tests for the graph-aware embedding pipeline (TD-006). Zero third-party deps."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import (  # noqa: E402
    DefaultSerializer,
    EmbeddingPipeline,
    HashingEmbedder,
    Node,
    Rel,
    Store,
)


def _build():
    s = Store(":memory:")
    nodes = {
        "MassNotificationJob": "triggers mass notifications",
        "send_notification": "send one notification via the queue",
        "RedisQueue": "redis queue",
        "PushProvider": "push gateway",
        "NotificationLog": "log of notifications",
    }
    for uid, body in nodes.items():
        s.upsert_node(Node(uid=uid, type="function", name=uid, body=body))
    for src, dst, rel in [
        ("MassNotificationJob", "send_notification", Rel.CALLS),
        ("send_notification", "RedisQueue", Rel.CALLS),
        ("send_notification", "PushProvider", Rel.CALLS),
        ("send_notification", "NotificationLog", Rel.WRITES),
    ]:
        s.upsert_edge(src, dst, rel)
    return s


class _CountingEmbedder:
    """Wraps HashingEmbedder and counts how many texts it embedded."""

    def __init__(self):
        self.inner = HashingEmbedder()
        self.count = 0

    def embed(self, texts):
        self.count += len(texts)
        return self.inner.embed(texts)


def test_refresh_clears_dirty_and_embeds_all():
    s = _build()
    assert len(s.dirty_nodes()) == 5  # nothing embedded yet
    rep = EmbeddingPipeline(s, HashingEmbedder()).refresh()
    assert rep.embedded == 5 and rep.failed == 0
    assert s.dirty_nodes() == []
    n = s.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert n == 5


def test_serialization_is_deterministic_and_role_aware():
    s = _build()
    ser = DefaultSerializer()
    nid = s.id_for("send_notification")
    a = ser.serialize(s, nid)
    b = ser.serialize(s, nid)
    assert a == b  # deterministic
    assert a.startswith("send_notification  (function)")
    assert "calls: PushProvider, RedisQueue" in a   # outgoing, sorted
    assert "writes: NotificationLog" in a
    assert "called_by: MassNotificationJob" in a    # incoming CALLS -> called_by


def test_incremental_reembed_only_dirty():
    s = _build()
    emb = _CountingEmbedder()
    pipe = EmbeddingPipeline(s, emb)
    pipe.refresh()
    assert emb.count == 5
    emb.count = 0
    # A new edge dirties exactly its two endpoints.
    s.upsert_edge("MassNotificationJob", "RedisQueue", Rel.USES)
    assert len(s.dirty_nodes()) == 2
    pipe.refresh()
    assert emb.count == 2  # only the two endpoints were re-embedded
    assert s.dirty_nodes() == []


def test_reembed_all():
    s = _build()
    emb = _CountingEmbedder()
    pipe = EmbeddingPipeline(s, emb)
    pipe.refresh()
    emb.count = 0
    pipe.reembed_all()
    assert emb.count == 5
    assert s.dirty_nodes() == []


def test_refresh_noop_when_clean():
    s = _build()
    pipe = EmbeddingPipeline(s, HashingEmbedder())
    pipe.refresh()
    rep = pipe.refresh()
    assert rep.embedded == 0 and rep.batches == 0


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        fn()
        print(f"ok  {name}")
    print(f"\nall green ({len(tests)} tests)")
