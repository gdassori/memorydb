"""Tests for the tree-sitter CodeAdapter (TD-005). Requires the [code] extra; skipped if absent."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_language_pack  # noqa: F401
    from memorydb.adapters.code import CodeAdapter, LanguageRegistry
    HAVE_CODE = True
except Exception:
    HAVE_CODE = False

pytestmark = pytest.mark.skipif(not HAVE_CODE, reason="[code] extra (tree-sitter) not installed")

PY_SRC = '''\
import os
from services.notifications import NotificationService


class Base:
    pass


class Worker(Base):
    """A worker that runs jobs."""

    def run(self, n):
        return self.handle(n)

    def handle(self, n):
        NotificationService().send(n)
        os.getpid()
'''


def _extract(src: str, filename: str):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, filename)
        with open(path, "w") as fh:
            fh.write(src)
        return CodeAdapter(repo_root=d).extract(path), filename


def test_registry_by_extension():
    reg = LanguageRegistry()
    assert reg.spec_for("a/b.py").name == "python"
    assert reg.spec_for("a/b.go").name == "go"
    assert reg.spec_for("a/b.ts").name == "typescript"
    assert reg.spec_for("a/b.txt") is None


def test_python_nodes_extracted():
    ex, fn = _extract(PY_SRC, "mod.py")
    by_uid = {n.uid: n for n in ex.nodes}
    assert f"{fn}::Base" in by_uid and by_uid[f"{fn}::Base"].type == "class"
    assert by_uid[f"{fn}::Worker"].type == "class"
    assert by_uid[f"{fn}::Worker.run"].type == "method"
    assert by_uid[f"{fn}::Worker.handle"].type == "method"
    # attrs: signature + docstring + file_uid
    w = by_uid[f"{fn}::Worker"]
    assert w.attrs["docstring"] == "A worker that runs jobs."
    assert by_uid[f"{fn}::Worker.run"].attrs["signature"].startswith("def run")
    assert by_uid[f"{fn}::Worker.run"].attrs["file_uid"] == fn


def test_python_in_file_edges_high_confidence():
    ex, fn = _extract(PY_SRC, "mod.py")
    e = {(x.src, x.dst, x.relation): x for x in ex.edges}
    # self.handle(n) resolves to the same-file method -> 0.9
    assert (f"{fn}::Worker.run", f"{fn}::Worker.handle", "CALLS") in e
    assert e[(f"{fn}::Worker.run", f"{fn}::Worker.handle", "CALLS")].confidence == 0.9
    # inheritance Worker -> Base resolves in-file -> 0.9
    assert (f"{fn}::Worker", f"{fn}::Base", "INHERITS") in e


def test_python_import_scoped_pending():
    ex, fn = _extract(PY_SRC, "mod.py")
    names = {(name, conf) for (_src, name, _rel, conf) in ex.pending}
    # NotificationService is imported -> import-scoped 0.6 (not a bare 0.3)
    assert ("NotificationService", 0.6) in names
    # os.getpid(): object `os` is imported -> import-scoped
    assert ("getpid", 0.6) in names


def test_unsupported_language_is_skipped():
    ex, _ = _extract("hello world", "notes.txt")
    assert ex.nodes == [] and ex.edges == [] and ex.pending == []


def test_javascript_smoke():
    js = "import {q} from 'q';\nclass C { run() { helper(); } }\nfunction helper() {}\n"
    ex, fn = _extract(js, "app.js")
    uids = {n.uid for n in ex.nodes}
    assert f"{fn}::helper" in uids          # top-level function
    assert any(u.endswith("::C") for u in uids)  # class C
