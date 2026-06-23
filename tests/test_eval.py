"""Eval-harness tests (eval-harness spec). Metric math + compare are pure zero-dep; the inline-fake
end-to-end exercises the whole Evaluator with no extras; the sample-suite e2e needs the [code] extra."""
from __future__ import annotations

import math
import os
import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import MemoryDB, Node  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402
from memorydb.eval import (  # noqa: E402
    EvalCase, Evaluator, Scorecard, compare,
    f1, mrr, ndcg_at_k, precision, recall, recall_at_k,
)

APPROX = 1e-6


def _close(a, b):
    return abs(a - b) <= APPROX


# --- pure metric math ------------------------------------------------------
def test_metrics_math():
    returned, expected = ["a", "b", "c"], ["a", "c", "d"]
    assert _close(precision(returned, expected), 2 / 3)
    assert _close(recall(returned, expected), 2 / 3)
    assert _close(f1(2 / 3, 2 / 3), 2 / 3)
    assert _close(recall_at_k(returned, expected, 2), 1 / 3)   # top-2 = {a,b}, hits {a}
    assert _close(mrr(returned, expected), 1.0)                # 'a' relevant at rank 1
    idcg = 1 + 1 / math.log2(3) + 1 / 2
    dcg = 1 + 1 / 2                                            # rel a@1, b@2=0, c@3
    assert _close(ndcg_at_k(returned, expected, 3), dcg / idcg)

    # edge cases (no div-by-zero)
    assert precision([], ["a"]) == 0.0
    assert recall(["a"], []) == 1.0
    assert f1(0.0, 0.0) == 0.0
    assert mrr(["x", "y"], ["a"]) == 0.0
    assert ndcg_at_k([], ["a"], 5) == 0.0


def test_ndcg_graded_gains():
    # graded relevance overrides binary: a high-gain item ranked first scores best
    ranked = ["a", "b"]
    gains = {"a": 3.0, "b": 1.0}
    val = ndcg_at_k(ranked, ["a", "b"], 2, gains)
    assert _close(val, 1.0)                                    # already ideal order


# --- baseline compare ------------------------------------------------------
def test_baseline_compare():
    base = Scorecard(locate={"precision": 0.80, "recall": 0.70, "f1": 0.75, "n": 5},
                     explain={"recall_at_k": 0.60, "mrr": 0.50, "ndcg": 0.55, "n": 3})
    new = Scorecard(locate={"precision": 0.85, "recall": 0.70, "f1": 0.77, "n": 5},
                    explain={"recall_at_k": 0.58, "mrr": 0.52, "ndcg": 0.55, "n": 3})
    d = compare(base, new)
    assert _close(d["locate"]["precision"], 0.05)
    assert _close(d["locate"]["recall"], 0.0)
    assert _close(d["explain"]["recall_at_k"], -0.02)
    assert "n" not in d["locate"]                              # counts aren't deltas


def test_scorecard_roundtrip():
    card = Scorecard(locate={"precision": 1.0, "n": 2}, explain={"mrr": 0.5, "n": 1}, k=7,
                     per_case=[{"query": "x"}], broken=["y"])
    assert Scorecard.from_dict(card.to_dict()).k == 7
    assert Scorecard.from_dict(card.to_dict()).broken == ["y"]


# --- inline fake end-to-end (zero-dep) -------------------------------------
class FakeExtractor:
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
            return Extraction(nodes=[Node(uid=f"{rel}::g", type="function", name="g",
                                          body="def g(): return foo()", attrs={"file_uid": rel})],
                              pending=[(f"{rel}::g", "foo", "CALLS", 0.6)])
        if base == "b.fake":
            return Extraction(nodes=[Node(uid=f"{rel}::foo", type="function", name="foo",
                                          body="def foo(): return 1", attrs={"file_uid": rel})])
        return Extraction()


def _fake_repo():
    d = tempfile.mkdtemp()
    for name in ("a.fake", "b.fake"):
        with open(os.path.join(d, name), "w") as fh:
            fh.write("v1")
    return d


def test_end_to_end_inline_fake():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        db = MemoryDB.open(":memory:", extractors=[FakeExtractor()])
    db.index(_fake_repo())
    cases = [
        EvalCase(query="foo", intent="LOCATE", expected_uids=["a.fake::g"]),
        EvalCase(query="foo", intent="EXPLAIN", expected_uids=["b.fake::foo"]),
        EvalCase(query="ghost", intent="LOCATE", expected_uids=["does.not::exist"]),  # broken: label drift
    ]
    card = Evaluator(db).run(cases, k=10)
    assert card.locate["precision"] == 1.0 and card.locate["recall"] == 1.0
    assert card.locate["n"] == 1                             # broken LOCATE excluded from aggregate
    assert card.broken == ["ghost"]
    assert card.explain["recall_at_k"] >= 0.5 and card.explain["n"] == 1
    db.close()


# --- sample suite end-to-end (needs the [code] extra) ----------------------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False

_SAMPLE = Path(__file__).resolve().parents[1] / "eval" / "suites" / "sample"


def test_end_to_end_sample():
    if not HAVE_CODE:
        print("skip test_end_to_end_sample: [code] extra not installed")
        return
    from memorydb.eval import evaluate_suite
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        card = evaluate_suite(str(_SAMPLE), k=10)
    assert not card.broken                                  # labels match the fixture
    assert card.locate["f1"] == 1.0                          # call graph is deterministic
    # The default extractors now include the PythonResolver, so the cross-file send_notification
    # caller is a PRECISE 0.97 edge (was a coarse 0.6 tree-sitter pending) — precision@>=0.9 is now
    # 1.0, up from 0.5. This is the eval harness measuring the python-precise-resolver's payoff.
    assert _close(card.locate["precision_high"], 1.0)
    assert card.explain["recall_at_k"] >= 0.5


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
