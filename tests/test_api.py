"""Facade tests (public-api-facade spec). Zero-dep: a deterministic FakeExtractor + HashingEmbedder,
so this runs under plain ``python tests/test_api.py`` with no extras installed."""
from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import ContextResult, HashingEmbedder, MemoryDB, Node  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402 (no tree-sitter dep on import)
from memorydb.models import Intent  # noqa: E402


# --- a deterministic fake extractor (zero-dep) -----------------------------
class FakeExtractor:
    """a.fake declares g() which calls foo(); b.fake declares foo(). Cross-file by name."""

    def __init__(self):
        self.repo_root = "."

    def handles(self, path):
        return path.endswith(".fake")

    def lang_of(self, path):
        return "fake"

    def extract(self, path):
        rel = os.path.relpath(path, self.repo_root).replace(os.sep, "/")
        base = os.path.basename(rel)
        if base == "a.fake":
            return Extraction(
                nodes=[Node(uid=f"{rel}::g", type="function", name="g",
                            body="def g(): return foo()", attrs={"file_uid": rel})],
                edges=[],
                pending=[(f"{rel}::g", "foo", "CALLS", 0.6)],
            )
        if base == "b.fake":
            return Extraction(
                nodes=[Node(uid=f"{rel}::foo", type="function", name="foo",
                            body="def foo(): return 1", attrs={"file_uid": rel})],
            )
        return Extraction()


def _repo():
    d = tempfile.mkdtemp()
    for name, text in (("a.fake", "v1"), ("b.fake", "v1")):
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)
    return d


def _open(**kw):
    # default to the FakeExtractor so the facade indexes without the [code] extra
    kw.setdefault("extractors", [FakeExtractor()])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return MemoryDB.open(":memory:", **kw)


def test_open_index_ask():
    db = _open()
    rep = db.index(_repo())
    assert rep.files_indexed == 2 and rep.nodes_upserted == 2
    assert rep.edges_upserted == 1 and rep.embedded >= 1

    loc = db.ask("where is foo used?")
    assert loc["intent"] == "LOCATE"
    assert "a.fake::g" in {r["src_uid"] for r in loc["references"]}

    exp = db.ask("how does foo work?")
    assert exp["intent"] == "EXPLAIN"
    assert exp["nodes"]  # vector seed + traversal returned something
    db.close()


def test_locate_and_explain_helpers():
    db = _open()
    db.index(_repo())
    refs = db.locate("foo")
    assert [r["src_uid"] for r in refs] == ["a.fake::g"]
    sub = db.explain("how does foo work?")
    assert sub["intent"] == "EXPLAIN" and "nodes" in sub and "edges" in sub
    db.close()


def test_ports_overridable():
    calls = {"embed": 0, "classify": []}

    class FakeEmbedder:
        dim = 8

        def embed(self, texts):
            calls["embed"] += 1
            return [[float(len(t) % 7)] * self.dim for t in texts]

    class FakeClassifier:
        def classify(self, query):
            calls["classify"].append(query)
            return Intent.EXPLAIN

    db = _open(embedder=FakeEmbedder(), classifier=FakeClassifier())
    db.index(_repo())
    assert calls["embed"] > 0                       # our embedder did the embedding
    db.ask("anything at all")
    assert calls["classify"] == ["anything at all"]  # our classifier did the routing
    db.close()


def test_context_budget():
    db = _open()
    db.index(_repo())
    full = db.context("how does foo work?", budget_tokens=10_000)
    assert isinstance(full, ContextResult) and full.text and not full.truncated
    assert full.used_tokens <= full.budget_tokens

    tight = db.context("how does foo work?", budget_tokens=max(1, full.used_tokens // 2))
    assert tight.used_tokens <= tight.budget_tokens
    assert tight.truncated and len(tight.uids) < len(full.uids)

    packed = db.ask("how does foo work?", as_context=True, budget_tokens=10_000)
    assert isinstance(packed, ContextResult) and packed.intent == "EXPLAIN"
    db.close()


def test_defaults_present():
    # open() with no ports yields a working instance (warns about HashingEmbedder / [code]).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        db = MemoryDB.open(":memory:")
    assert db.ask("how does anything work?")["intent"] == "EXPLAIN"   # empty store, no error
    assert db.locate("nope") == []
    assert db.store is not None and db.planner is not None
    db.close()


def test_embedder_change_warns():
    store_path = os.path.join(tempfile.mkdtemp(), "repo.db")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        MemoryDB.open(store_path, embedder=HashingEmbedder(dim=64), extractors=[FakeExtractor()]).close()

    class OtherEmbedder:
        dim = 128

        def embed(self, texts):
            return [[0.0] * self.dim for _ in texts]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        MemoryDB.open(store_path, embedder=OtherEmbedder(), extractors=[FakeExtractor()]).close()
    msgs = " ".join(str(w.message) for w in caught)
    assert "embedder changed" in msgs and "dim changed" in msgs


def test_use_after_close():
    db = _open()
    db.close()
    for call in (lambda: db.ask("x"), lambda: db.index("."), lambda: db.locate("y")):
        try:
            call()
            assert False, "expected RuntimeError after close()"
        except RuntimeError:
            pass
    db.close()  # idempotent


if __name__ == "__main__":
    tests = {n: f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)}
    for name, fn in tests.items():
        try:
            fn()
            print(f"ok  {name}")
        except Exception as e:  # noqa
            import traceback
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
    print("done")
