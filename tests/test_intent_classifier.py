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


# --- mega-review (P4) regressions --------------------------------------------

def _emb():
    class _E:
        dim = 8
        def embed(self, texts):
            return [[0.0] * self.dim for _ in texts]
    return _E()


def test_p4_1_non_scalar_filter_value_dropped_not_crash():
    """A list/dict FILTER value (an LLM can return one) must be dropped like an unknown key — binding
    it would raise sqlite3.ProgrammingError out of the public API (violating 'never raise')."""
    sql, params, dropped = build_filter_query({"lang": ["go", "py"], "type": {"x": 1}, "path_glob": "p/*"})
    assert "lang" in dropped and "type" in dropped               # non-scalars dropped
    assert params == {"path_glob": "p/*"} and ":lang" not in (sql or "")
    # end-to-end: a FILTER reply with a list value must not raise
    store = Store(":memory:")
    store.upsert_node(Node(uid="p/x.go::F", type="function", name="F",
                           attrs={"lang": "go", "file_uid": "p/x.go"}))
    store.commit()
    payload = _json("FILTER", filters={"lang": ["go", "python"]}, confidence=0.9)
    out = RetrievalPlanner(store, _emb(), classifier=LLMIntentClassifier(FakeLLM(payload))).retrieve("q")
    assert out["intent"] == "FILTER" and out["dropped_keys"] == ["lang"]   # no crash, key dropped
    store.close()


def test_p4_2_bare_year_since_is_rejected_not_epoch():
    """A bare-year/numeric string `since` ("2026") must NOT be read as epoch 2026.0 (~1970) which would
    match everything; strings are parsed as ISO dates only."""
    # bare year / float-notation / 10-digit epoch string: none are valid ISO dates -> dropped, NOT read
    # as a ~1970 epoch (across 3.10/3.11/3.12). ("20260615" IS valid ISO basic-format in 3.11+, so it is
    # deliberately not here — parsing it as 2026-06-15 is correct.)
    for bad in ("2026", "1e9", "1700000000", "lastweek"):
        sql, params, dropped = build_filter_query({"lang": "go", "since": bad})
        assert "since" in dropped and "since" not in params, bad
    # a proper ISO date is accepted as a float epoch
    sql, params, _ = build_filter_query({"since": "2026-06-15"})
    assert isinstance(params["since"], float)
    # a real numeric epoch (int/float type, not string) is accepted
    _, params2, _ = build_filter_query({"since": 1_700_000_000})
    assert params2["since"] == 1_700_000_000.0


def test_p4_2_nan_inf_since_dropped():
    for bad in (float("nan"), float("inf")):
        _, params, dropped = build_filter_query({"lang": "go", "since": bad})
        assert "since" in dropped and "since" not in params


def test_p4_3_since_excludes_unknown_mtime_without_dropping_others():
    store = Store(":memory:")
    # file with a known recent mtime + its symbol; and a file with mtime=None + its symbol
    store.upsert_node(Node(uid="recent.go", type="file", name="recent.go",
                           attrs={"lang": "go", "mtime": 1_800_000_000.0}))
    store.upsert_node(Node(uid="recent.go::A", type="function", name="A",
                           attrs={"lang": "go", "file_uid": "recent.go"}))
    store.upsert_node(Node(uid="nomtime.go", type="file", name="nomtime.go",
                           attrs={"lang": "go", "mtime": None}))
    store.upsert_node(Node(uid="nomtime.go::B", type="function", name="B",
                           attrs={"lang": "go", "file_uid": "nomtime.go"}))
    store.commit()
    # lang-only: both functions
    sql, params, _ = build_filter_query({"lang": "go"})
    assert {store.get_nodes([r[0] for r in store.conn.execute(sql, params)])[i]["name"]
            for i in (0, 1)} == {"A", "B"}
    # lang + since: only A (known recent mtime); B (unknown mtime) excluded, NOT a crash
    sql, params, _ = build_filter_query({"lang": "go", "since": "2020-01-01"})
    ids = [r[0] for r in store.conn.execute(sql, params).fetchall()]
    assert [n["name"] for n in store.get_nodes(ids)] == ["A"]
    store.close()


def test_p4_4_symbol_guard_is_fresh_not_stale_across_index():
    store = Store(":memory:")
    store.commit()
    planner = RetrievalPlanner(store, _emb(),
                               classifier=LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Gamma", confidence=0.99))))
    assert planner.retrieve("where is Gamma used?")["intent"] == "EXPLAIN"   # absent -> downgraded
    store.upsert_node(Node(uid="g.py::Gamma", type="function", name="Gamma", attrs={"lang": "python"}))
    store.commit()
    assert planner.retrieve("where is Gamma used?")["intent"] == "LOCATE"    # now present -> fresh LOCATE
    store.close()


def test_p4_5_shared_classifier_two_planners_own_stores():
    storeA, storeB = Store(":memory:"), Store(":memory:")
    storeA.upsert_node(Node(uid="a.py::Beta", type="function", name="Beta", attrs={"lang": "python"}))
    storeA.commit(); storeB.commit()
    shared = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Beta", confidence=0.99)))
    pA = RetrievalPlanner(storeA, _emb(), classifier=shared)
    pB = RetrievalPlanner(storeB, _emb(), classifier=shared)
    assert pA.retrieve("where is Beta used?")["intent"] == "LOCATE"    # Beta in storeA
    assert pB.retrieve("where is Beta used?")["intent"] == "EXPLAIN"   # Beta absent in storeB
    assert shared.symbol_exists is None                               # injected classifier not mutated
    storeA.close(); storeB.close()


def test_p4_6_path_glob_matches_file_path_not_uid():
    store = Store(":memory:")
    store.upsert_node(Node(uid="pkg/queue/foo.py::handle", type="function", name="handle",
                           attrs={"lang": "python", "file_uid": "pkg/queue/foo.py"}))
    store.commit()
    for glob in ("pkg/queue/*.py", "pkg/queue/foo.py", "*.py", "pkg/queue/*"):
        sql, params, _ = build_filter_query({"path_glob": glob})
        ids = [r[0] for r in store.conn.execute(sql, params).fetchall()]
        assert [n["name"] for n in store.get_nodes(ids)] == ["handle"], glob   # file-anchored globs work
    store.close()


def test_p4_7_lowercase_intent_normalized():
    c = LLMIntentClassifier(FakeLLM(_json("locate", symbol="Foo", confidence=0.96)))
    r = c.analyze("where is Foo used?")
    assert r.intent is Intent.LOCATE and r.symbol == "Foo"           # not discarded to regex fallback
    c2 = LLMIntentClassifier(FakeLLM(_json("Filter", filters={"lang": "go"}, confidence=0.9)))
    assert c2.analyze("q").intent is Intent.FILTER


def test_p4_intent_result_frozen():
    import pytest
    r = IntentResult(intent=Intent.LOCATE)
    with pytest.raises(Exception):
        r.intent = Intent.EXPLAIN                                    # frozen -> cannot mutate a cached result


def test_p4_cache_is_bounded():
    fake = FakeLLM(_json("EXPLAIN", confidence=0.9))
    c = LLMIntentClassifier(fake, max_cache=10)
    for i in range(25):
        c.analyze(f"query {i}")
    assert len(c.cache) <= 10                                        # evicts oldest, no unbounded growth


def test_p4_symbol_exists_exception_does_not_raise():
    def boom(_s):
        raise RuntimeError("store exploded")
    c = LLMIntentClassifier(FakeLLM(_json("LOCATE", symbol="Foo", confidence=0.99)), symbol_exists=boom)
    assert c.analyze("where is Foo used?").intent is Intent.LOCATE   # guard error swallowed, never raises


def test_p4_filter_respects_k_limit():
    store = Store(":memory:")
    for i in range(10):
        store.upsert_node(Node(uid=f"a.py::f{i}", type="function", name=f"f{i}", attrs={"lang": "go"}))
    store.commit()
    payload = _json("FILTER", filters={"lang": "go"}, confidence=0.95)
    planner = RetrievalPlanner(store, _emb(), classifier=LLMIntentClassifier(FakeLLM(payload)))
    out = planner.retrieve("go", k=3)
    assert len(out["nodes"]) == 3 and out["truncated"] is True       # capped at k, and the cap is signalled
    out_all = planner.retrieve("go", k=50)
    assert len(out_all["nodes"]) == 10 and out_all["truncated"] is False   # fits -> not truncated
    store.close()


# --- second-round (P4R) regressions ------------------------------------------

def test_p4r_1_giant_int_since_does_not_raise():
    """A huge-int `since` (an LLM can emit 10**400 as a JSON literal) overflowed float()/math.isfinite
    and raised OverflowError out of ask() — the P4-2 finiteness guard was incomplete (P4R-1)."""
    from memorydb.filters import _to_epoch
    assert _to_epoch(10 ** 400) is None and _to_epoch(-(10 ** 400)) is None
    sql, params, dropped = build_filter_query({"lang": "go", "since": 10 ** 400})
    assert "since" in dropped and "since" not in params
    # end-to-end: must not raise
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.go::F", type="function", name="F",
                           attrs={"lang": "go", "file_uid": "a.go"}))
    store.commit()
    payload = _json("FILTER", filters={"lang": "go", "since": 10 ** 400}, confidence=0.9)
    out = RetrievalPlanner(store, _emb(), classifier=LLMIntentClassifier(FakeLLM(payload))).retrieve("q")
    assert out["intent"] == "FILTER" and "since" in out["dropped_keys"]
    store.close()


def test_p4r_2_since_parsing_is_version_independent():
    """`since` parsing must be identical on 3.10/3.11/3.12: Z-suffix and basic-format dates are parsed
    the same everywhere (strptime, not fromisoformat whose grammar widened in 3.11 — P4R-2)."""
    from memorydb.filters import _to_epoch
    iso = _to_epoch("2026-06-15")
    assert isinstance(iso, float)
    assert _to_epoch("2026-06-15T00:00:00Z") == iso                 # Z-suffix UTC midnight == the date
    assert _to_epoch("20260615") == iso                             # basic format, consistently accepted
    assert _to_epoch("2026-06-15T00:00:00+00:00") == iso            # explicit offset
    for bad in ("2026", "2026-W24", "1e9", "lastweek"):
        assert _to_epoch(bad) is None, bad                          # rejected identically everywhere


def test_p4r_3_filter_dict_copy_does_not_corrupt_cache():
    """The returned FILTER `filters` dict must be a copy, so a caller mutating it can't corrupt the
    cached IntentResult (frozen only blocks attribute reassignment, not container mutation — P4R-3)."""
    store = Store(":memory:")
    store.upsert_node(Node(uid="a.go::F", type="function", name="F",
                           attrs={"lang": "go", "file_uid": "a.go"}))
    store.commit()
    payload = _json("FILTER", filters={"lang": "go"}, confidence=0.95)
    planner = RetrievalPlanner(store, _emb(), classifier=LLMIntentClassifier(FakeLLM(payload)))
    out1 = planner.retrieve("list go")
    out1["filters"].clear()                                         # mutate the returned dict
    out2 = planner.retrieve("list go")                             # identical query -> cache hit
    assert out2["filters"] == {"lang": "go"}                        # cache NOT corrupted
    store.close()


# --- third-round (P4R3) regressions ------------------------------------------

def test_p4r3_1_minute_precision_since_parses():
    """Minute-precision (no-seconds) ISO datetimes that fromisoformat accepted must still parse, else
    the `since` predicate silently drops and FILTER broadens to all ages (P4R3-1)."""
    from memorydb.filters import _to_epoch
    base = _to_epoch("2026-06-15T14:30:00")
    assert isinstance(base, float)
    assert _to_epoch("2026-06-15T14:30") == base                    # no seconds
    assert _to_epoch("2026-06-15 14:30") == base                    # space, no seconds
    assert _to_epoch("2026-06-15T16:30+02:00") == base              # 16:30+02:00 == 14:30 UTC == base


def test_p4r3_3_direct_analyze_cannot_corrupt_cache():
    """A direct analyze() caller mutating entities/filters of the returned result must not corrupt the
    cached value for the next same-query call (P4R3-3 — frozen alone didn't protect the containers)."""
    c = LLMIntentClassifier(FakeLLM(_json("FILTER", entities=["a", "b"], filters={"lang": "go"}, confidence=0.9)))
    r1 = c.analyze("q")
    r1.entities.append("X")
    r1.filters["injected"] = "v"
    r2 = c.analyze("q")                                             # same-query cache hit
    assert r2.entities == ["a", "b"] and r2.filters == {"lang": "go"}   # cache intact


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
