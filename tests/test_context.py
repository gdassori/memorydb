"""ContextBuilder tests (context-builder-packing spec). Fully zero-dep (HeuristicCounter)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import ContextBuilder, ContextResult, HeuristicCounter  # noqa: E402


def _explain():
    return {
        "intent": "EXPLAIN",
        "seeds": [1, 2],
        "depths": {1: 0, 2: 0, 3: 1, 4: 2},
        "nodes": [
            {"id": 1, "uid": "a.py::send", "type": "function", "name": "send",
             "attrs": {"file_uid": "a.py", "start_line": 10, "signature": "def send(u, m):",
                       "docstring": "Send a notification."}},
            {"id": 2, "uid": "a.py::enqueue", "type": "function", "name": "enqueue",
             "attrs": {"file_uid": "a.py", "start_line": 20, "signature": "def enqueue(m):",
                       "docstring": "Queue it."}},
            {"id": 3, "uid": "b.py::Job", "type": "class", "name": "Job",
             "attrs": {"file_uid": "b.py", "start_line": 5, "signature": "class Job:"}},
            {"id": 4, "uid": "c.py::far", "type": "function", "name": "far",
             "attrs": {"file_uid": "c.py", "start_line": 1}},
        ],
        "edges": [
            {"src": "a.py::send", "dst": "a.py::enqueue", "relation": "CALLS", "confidence": 1.0},
            {"src": "b.py::Job", "dst": "a.py::send", "relation": "CALLS", "confidence": 0.97},
        ],
    }


def test_respects_budget():
    for budget in (40, 80, 200, 1000):
        res = ContextBuilder().build(_explain(), budget)
        assert res.used_tokens <= budget, (budget, res.used_tokens)


def test_ordering_seeds_and_score_first():
    res = ContextBuilder().build(_explain(), 1000)
    assert res.uids[0] in ("a.py::send", "a.py::enqueue")       # a seed ranks first
    if "c.py::far" in res.uids:                                 # the depth-2, edge-less node ranks last
        assert res.uids[-1] == "c.py::far"


def test_provenance_present():
    res = ContextBuilder().build(_explain(), 1000)
    locs = {"a.py::send": "a.py:10", "a.py::enqueue": "a.py:20", "b.py::Job": "b.py:5", "c.py::far": "c.py:1"}
    for uid in res.uids:
        assert locs[uid] in res.text                            # every card carries file:line


def test_deterministic():
    a = ContextBuilder().build(_explain(), 300)
    b = ContextBuilder().build(_explain(), 300)
    assert a.text == b.text and a.uids == b.uids and a.used_tokens == b.used_tokens


def test_reports_dropped():
    res = ContextBuilder().build(_explain(), 30)                # too small for all four cards
    assert res.dropped > 0 and res.truncated
    assert res.used_tokens <= 30 and len(res.uids) < 4          # explicit, not silently cut


def test_relationships_block_rendered():
    res = ContextBuilder().build(_explain(), 1000)
    assert "**Relationships**" in res.text
    assert "Job --CALLS--> send" in res.text                    # qualnames, an included edge


def test_locate_used_at_list():
    res = ContextBuilder().build({
        "intent": "LOCATE", "symbol": "send",
        "references": [{"src_uid": "b.py::Job.run", "src_name": "run", "relation": "CALLS", "confidence": 0.97}],
    }, 1000)
    assert res.intent == "LOCATE" and "used at:" in res.text and "run" in res.text


def test_empty_result():
    res = ContextBuilder().build({"intent": "EXPLAIN", "nodes": []}, 1000)
    assert isinstance(res, ContextResult) and res.text == "" and res.used_tokens == 0


def test_pluggable_token_counter():
    class WordCounter:
        def count(self, text):
            return max(1, len(text.split()))
    res = ContextBuilder(counter=WordCounter()).build(_explain(), 1000)
    # used_tokens is now a word count; sanity: it differs from the chars/4 heuristic and respects budget
    assert res.used_tokens <= 1000 and res.used_tokens != HeuristicCounter().count(res.text)


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
