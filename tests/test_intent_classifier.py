"""LLM intent classifier + FILTER builder tests (llm-intent-classifier spec). Fully zero-dep:
a ``FakeLLM`` returns canned JSON; the FILTER builder is exercised both in isolation and against a
real in-memory Store to prove parameterization/injection-safety."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import (  # noqa: E402
    DefaultIntentClassifier, Intent, IntentResult, LLMIntentClassifier, Node, RetrievalPlanner, Store,
    build_filter_query,
)


class FakeLLM:
    """Canned LLMClient: returns the configured text, or raises if it is an Exception."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _json(intent, **kw):
    return json.dumps({"intent": intent, **kw})


# --- routing -----------------------------------------------------------------

def test_routes_locate():
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Foo", confidence=0.96)))
    r = c.analyze("where is Foo used?")
    assert r.intent is Intent.LOCATE and r.symbol == "Foo" and c.classify("where is Foo used?") is Intent.LOCATE


def test_routes_explain():
    c = LLMIntentClassifier(FakeLLM(_json("EXPLAIN", entities=["mass notification"], confidence=0.9)))
    r = c.analyze("how do mass notifications work?")
    assert r.intent is Intent.EXPLAIN and r.entities == ["mass notification"]


def test_routes_filter():
    payload = _json("FILTER", filters={"type": "function", "lang": "go", "path_glob": "pkg/queue/*",
                                       "since": "2026-06-15"}, confidence=0.92)
    r = LLMIntentClassifier(FakeLLM(payload)).analyze("show me Go functions in pkg/queue since 2026-06-15")
    assert r.intent is Intent.FILTER and r.filters["lang"] == "go" and r.filters["path_glob"] == "pkg/queue/*"


def test_tolerates_json_fence_and_prose():
    reply = "Sure!\n```json\n" + _json("LOCATE", symbol="Bar", confidence=0.8) + "\n```\n"
    assert LLMIntentClassifier(FakeLLM(reply)).analyze("q").intent is Intent.LOCATE


# --- FILTER → SQL safety -----------------------------------------------------

def test_filter_sql_is_parameterized():
    sql, params, dropped = build_filter_query({"type": "function", "lang": "go",
                                               "path_glob": "pkg/*", "since": "2026-06-15"})
    assert ":type" in sql and ":lang" in sql and ":path_glob" in sql and ":since" in sql
    # no value is interpolated into the SQL text — every value lives in params, bound
    assert "function" not in sql and "go" not in sql and "pkg/*" not in sql
    assert params["type"] == "function" and params["lang"] == "go" and params["path_glob"] == "pkg/*"
    assert isinstance(params["since"], float) and not dropped         # since coerced to epoch


def test_filter_unknown_and_empty_keys_dropped():
    sql, params, dropped = build_filter_query({"type": "class", "evil": "1=1", "lang": ""})
    assert sql is not None and params == {"type": "class"}            # only the valid, non-empty key
    assert set(dropped) == {"evil", "lang"}


def test_filter_no_usable_predicate_returns_none():
    sql, params, dropped = build_filter_query({"nope": "x"})
    assert sql is None and params == {} and dropped == ["nope"]


def test_injection_neutralized():
    """A SQL-injection payload in a filter value is bound, not interpolated: the query runs and the
    nodes table is intact afterwards."""
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.py::f", type="function", name="f", attrs={"lang": "python"}))
    store.commit()
    sql, params, _ = build_filter_query({"type": "function'; DROP TABLE nodes;--"})
    rows = store.conn.execute(sql, params).fetchall()                 # executes safely, matches nothing
    assert rows == []
    # table still exists and still holds the node
    assert store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
    store.close()


def test_filter_end_to_end_matches_by_attrs():
    store = Store(":memory:")
    store.upsert_node(Node(uid="pkg/queue/q.go::Push", type="function", name="Push",
                           attrs={"lang": "go", "file_uid": "pkg/queue/q.go"}))
    store.upsert_node(Node(uid="pkg/queue/q.go::Klass", type="class", name="Klass",
                           attrs={"lang": "go", "file_uid": "pkg/queue/q.go"}))
    store.upsert_node(Node(uid="other/x.py::helper", type="function", name="helper",
                           attrs={"lang": "python", "file_uid": "other/x.py"}))
    store.commit()
    sql, params, _ = build_filter_query({"type": "function", "lang": "go", "path_glob": "pkg/queue/*"})
    ids = [r[0] for r in store.conn.execute(sql, params).fetchall()]
    names = {store.get_nodes(ids)[0]["name"]}
    assert names == {"Push"}                                          # only the Go function under pkg/queue
    store.close()


# --- fallback & guards (planner integration where store-backed) --------------

def test_fallback_on_llm_error():
    c = LLMIntentClassifier(FakeLLM(RuntimeError("timeout")), fallback=DefaultIntentClassifier())
    # regex fallback classifies "where is X used?" as LOCATE, never raises
    assert c.analyze("where is X used?").intent is Intent.LOCATE
    assert c.analyze("how does X work?").intent is Intent.EXPLAIN


def test_fallback_on_invalid_json():
    assert LLMIntentClassifier(FakeLLM("not json at all")).analyze("how does X work?").intent is Intent.EXPLAIN


def test_out_of_range_confidence_falls_back():
    # confidence 1.5 violates the [0,1] schema -> parse failure -> regex fallback (EXPLAIN here)
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Z", confidence=1.5)))
    assert c.analyze("how does Z work?").intent is Intent.EXPLAIN


def test_low_confidence_forces_explain():
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Foo", confidence=0.3)))
    assert c.analyze("where is Foo used?").intent is Intent.EXPLAIN   # < 0.5 -> safe richer path


def test_hallucinated_symbol_downgraded():
    # symbol_exists says the symbol is absent -> LOCATE downgrades to EXPLAIN
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Ghost", confidence=0.99)),
                            symbol_exists=lambda s: False)
    assert c.analyze("where is Ghost used?").intent is Intent.EXPLAIN
    # and when it exists, it stays LOCATE
    c2 = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Real", confidence=0.99)),
                             symbol_exists=lambda s: s == "Real")
    assert c2.analyze("where is Real used?").intent is Intent.LOCATE


def test_caches_by_query():
    fake = FakeLLM(_json("EXPLAIN", confidence=0.9))
    c = LLMIntentClassifier(fake)
    c.analyze("same query"); c.analyze("same query")
    assert fake.calls == 1                                            # second call served from cache


# --- planner wiring ----------------------------------------------------------

def test_planner_routes_filter_end_to_end():
    store = Store(":memory:")
    store.upsert_node(Node(uid="pkg/q.go::Push", type="function", name="Push",
                           attrs={"lang": "go", "file_uid": "pkg/q.go"}))
    store.commit()

    class _Emb:
        dim = 8
        def embed(self, texts):
            return [[0.0] * self.dim for _ in texts]

    payload = _json("FILTER", filters={"type": "function", "lang": "go"}, confidence=0.95)
    planner = RetrievalPlanner(store, _Emb(), classifier=LLMIntentClassifier(FakeLLM(payload)))
    out = planner.retrieve("go functions")
    assert out["intent"] == "FILTER" and [n["name"] for n in out["nodes"]] == ["Push"]
    store.close()


def test_planner_wires_symbol_guard_to_store():
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.py::Real", type="function", name="Real", attrs={"lang": "python"}))
    store.commit()

    class _Emb:
        dim = 8
        def embed(self, texts):
            return [[0.0] * self.dim for _ in texts]

    # the planner auto-wires symbol_exists -> store; "Ghost" is absent so LOCATE downgrades to EXPLAIN
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Ghost", confidence=0.99)))
    planner = RetrievalPlanner(store, _Emb(), classifier=c)
    assert planner.retrieve("where is Ghost used?")["intent"] == "EXPLAIN"
    # a real symbol stays LOCATE and resolves
    c2 = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Real", confidence=0.99)))
    planner2 = RetrievalPlanner(store, _Emb(), classifier=c2)
    assert planner2.retrieve("where is Real used?")["intent"] == "LOCATE"
    store.close()


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
