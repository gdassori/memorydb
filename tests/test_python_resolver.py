"""PythonResolver tests (python-precise-resolver spec). Fully zero-dep (ast + symtable are stdlib),
except the [code]-gated test that checks uid-consistency + supersession against the real CodeAdapter."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import Indexer, Node, Store  # noqa: E402
from memorydb.adapters.code.python_resolver import PythonResolver  # noqa: E402


def _repo(files: dict) -> str:
    d = tempfile.mkdtemp()
    for name, text in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        with open(p, "w") as fh:
            fh.write(text)
    return d


def _edges(repo: str, name: str):
    """{(src, relation, dst): confidence} for one file."""
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, name))
    return {(e.src, e.relation, e.dst): e.confidence for e in ex.edges}


# --- resolution tiers -------------------------------------------------------
def test_resolves_direct_call():
    repo = _repo({"m.py": "def a():\n    return b()\n\ndef b():\n    return 1\n"})
    e = _edges(repo, "m.py")
    assert e[("m.py::a", "CALLS", "m.py::b")] == 1.0


def test_resolves_imported_call():
    repo = _repo({
        "b.py": "def foo():\n    return 1\n",
        "a.py": "from b import foo\n\ndef g():\n    return foo()\n",
    })
    e = _edges(repo, "a.py")
    assert e[("a.py::g", "CALLS", "b.py::foo")] == 0.97   # correct cross-file dst uid


def test_resolves_imported_call_with_alias_and_package():
    repo = _repo({
        "services/notifications.py": "def send():\n    return 1\n",
        "app/jobs.py": "from services.notifications import send as s\n\ndef run():\n    return s()\n",
    })
    e = _edges(repo, "app/jobs.py")
    assert e[("app/jobs.py::run", "CALLS", "services/notifications.py::send")] == 0.97


def test_resolves_module_attribute():
    repo = _repo({
        "util.py": "def helper():\n    return 1\n",
        "main.py": "import util\n\ndef go():\n    return util.helper()\n",
    })
    e = _edges(repo, "main.py")
    assert e[("main.py::go", "CALLS", "util.py::helper")] == 0.95


def test_self_method():
    repo = _repo({"m.py": "class C:\n    def a(self):\n        return self.b()\n    def b(self):\n        return 1\n"})
    e = _edges(repo, "m.py")
    assert e[("m.py::C.a", "CALLS", "m.py::C.b")] == 0.92


def test_inheritance_local_and_imported():
    repo = _repo({
        "base.py": "class Base:\n    pass\n",
        "m.py": "from base import Base\n\nclass Local:\n    pass\n\nclass A(Base):\n    pass\n\nclass B(Local):\n    pass\n",
    })
    e = _edges(repo, "m.py")
    assert e[("m.py::A", "INHERITS", "base.py::Base")] == 0.97   # imported base
    assert e[("m.py::B", "INHERITS", "m.py::Local")] == 1.0      # local base


def test_star_import_low_confidence():
    repo = _repo({
        "lib.py": "def thing():\n    return 1\n",
        "m.py": "from lib import *\n\ndef g():\n    return thing()\n",
    })
    e = _edges(repo, "m.py")
    assert e[("m.py::g", "CALLS", "lib.py::thing")] == 0.5


def test_unresolvable_attribute_skipped():
    # obj is a parameter of unknown type -> obj.method() must NOT produce an edge (no false positive)
    repo = _repo({"m.py": "def g(obj):\n    return obj.method()\n"})
    e = _edges(repo, "m.py")
    assert e == {}


def test_local_variable_shadows_module_def():
    # `read` is a parameter here; the call read() must not resolve to the module-level def `read`
    repo = _repo({"m.py": "def read():\n    return 0\n\ndef g(read):\n    return read()\n"})
    e = _edges(repo, "m.py")
    assert ("m.py::g", "CALLS", "m.py::read") not in e          # symtable prevented the false edge
    assert e == {}


# --- supersession & safety --------------------------------------------------
def test_supersedes_coarse_at_store_level():
    # A coarse edge (0.6) is overwritten by the resolver's precise edge (0.97) via MAX-confidence upsert.
    repo = _repo({"b.py": "def foo():\n    return 1\n", "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    s.upsert_node(Node(uid="a.py::g", type="function", name="g", attrs={"file_uid": "a.py"}))
    s.upsert_node(Node(uid="b.py::foo", type="function", name="foo", attrs={"file_uid": "b.py"}))
    s.upsert_edge("a.py::g", "b.py::foo", "CALLS", confidence=0.6, source="treesitter")  # coarse
    assert s.conn.execute("SELECT confidence FROM edges").fetchone()[0] == 0.6
    ex = PythonResolver(repo_root=repo).extract(os.path.join(repo, "a.py"))
    edge = next(e for e in ex.edges if e.dst == "b.py::foo")
    s.upsert_edge(edge.src, edge.dst, edge.relation, confidence=edge.confidence, source=edge.source)
    assert s.conn.execute("SELECT confidence FROM edges").fetchone()[0] == 0.97  # precise won


def test_safe_by_construction_skips_nonexistent_target():
    # An import to a module that isn't in the repo -> the computed uid doesn't exist -> the indexer
    # skips the edge rather than creating a wrong one.
    repo = _repo({"a.py": "from nope import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    Indexer(s, [PythonResolver()]).index(repo)
    assert s.id_for("a.py::g") is not None
    assert s.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0


def test_indexer_end_to_end_precise_edges():
    repo = _repo({"b.py": "def foo():\n    return 1\n", "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    rep = Indexer(s, [PythonResolver()]).index(repo)
    assert rep.files_indexed == 2
    conf = s.conn.execute(
        "SELECT e.confidence FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
        "WHERE a.uid='a.py::g' AND b.uid='b.py::foo' AND e.relation='CALLS'"
    ).fetchone()
    assert conf is not None and conf[0] == 0.97


# --- [code]-gated: uid consistency + supersession over the real coarse adapter ----------------
try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb import HashingEmbedder
    from memorydb.adapters.code import CodeAdapter
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False


def test_precise_supersedes_coarse_via_both_adapters():
    if not HAVE_CODE:
        print("skip test_precise_supersedes_coarse_via_both_adapters: [code] not installed")
        return
    repo = _repo({"b.py": "def foo():\n    return 1\n", "a.py": "from b import foo\n\ndef g():\n    return foo()\n"})
    s = Store(":memory:")
    # CodeAdapter alone resolves the cross-file call as a coarse import-scoped pending edge (~0.6);
    # adding PythonResolver upgrades it to a precise 0.97 (uids match, so the edge merges by MAX-conf).
    Indexer(s, [CodeAdapter(), PythonResolver()], HashingEmbedder()).index(repo)
    rows = s.conn.execute(
        "SELECT e.confidence FROM edges e JOIN nodes a ON a.id=e.src JOIN nodes b ON b.id=e.dst "
        "WHERE a.uid='a.py::g' AND b.uid='b.py::foo' AND e.relation='CALLS'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == 0.97        # one merged edge, precise confidence
    # nodes were deduped across the two extractors (not double-upserted)
    assert s.conn.execute("SELECT COUNT(*) FROM nodes WHERE uid='a.py::g'").fetchone()[0] == 1


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
