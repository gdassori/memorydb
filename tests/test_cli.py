"""CLI tests (cli spec). Zero-dep: invoke ``main([...])`` in-process against a temp file DB, with a
FakeExtractor injected by monkeypatching ExtractorRegistry.default (the CLI builds its own facade).
Runs under pytest and standalone (capture via redirect_*, not capsys, so both work)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memorydb import cli  # noqa: E402
from memorydb import api  # noqa: E402
from memorydb.adapters.code import Extraction  # noqa: E402
from memorydb.models import Node  # noqa: E402


class FakeExtractor:
    """a.fake declares g() which calls foo(); b.fake declares foo()."""

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
    for name in ("a.fake", "b.fake"):
        with open(os.path.join(d, name), "w") as fh:
            fh.write("v1")
    return d


def _run(argv):
    """Run main(argv) capturing output. Returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    orig = api.ExtractorRegistry.default
    api.ExtractorRegistry.default = staticmethod(lambda: [FakeExtractor()])
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        api.ExtractorRegistry.default = orig
    return code, out.getvalue(), err.getvalue()


def _db():
    return os.path.join(tempfile.mkdtemp(), "cli.sqlite")


def test_index_then_query():
    db, repo = _db(), _repo()
    code, out, _ = _run(["--db", db, "index", repo])
    assert code == 0 and "indexed 2 files" in out and "1 edges" in out

    code, out, _ = _run(["--db", db, "query", "where is foo used?"])
    assert code == 0 and out.startswith("LOCATE foo")
    assert "g" in out and "CALLS" in out


def test_explain_text():
    db, repo = _db(), _repo()
    _run(["--db", db, "index", repo])
    code, out, _ = _run(["--db", db, "explain", "how does foo work?"])
    assert code == 0 and out.startswith("EXPLAIN") and "nodes" in out


def test_status():
    db, repo = _db(), _repo()
    _run(["--db", db, "index", repo])
    code, out, _ = _run(["--db", db, "status"])
    assert code == 0 and "nodes: 4" in out          # 2 file nodes (a.fake, b.fake) + 2 symbols (g, foo)
    assert "edges: 1" in out and "schema v" in out


def test_status_json():
    db, repo = _db(), _repo()
    _run(["--db", db, "index", repo])
    code, out, _ = _run(["--db", db, "status", "--json"])
    info = json.loads(out)
    assert code == 0 and info["edges"] == 1 and info["embeddings"] >= 1
    assert info["embed_model"] == "HashingEmbedder"


def test_query_json_is_valid():
    db, repo = _db(), _repo()
    _run(["--db", db, "index", repo])
    code, out, _ = _run(["--db", db, "query", "where is foo used?", "--json"])
    payload = json.loads(out)
    assert code == 0 and payload["intent"] == "LOCATE"
    assert any(r["src_uid"] == "a.fake::g" for r in payload["references"])


def test_query_context_budget():
    db, repo = _db(), _repo()
    _run(["--db", db, "index", repo])
    code, out, _ = _run(["--db", db, "query", "how does foo work?", "--context", "--budget", "10000"])
    assert code == 0 and out.startswith("# context (EXPLAIN)")


def test_no_data_hint():
    code, out, err = _run(["--db", _db(), "query", "anything"])
    assert code == 0 and "no data" in err and out == ""


def test_usage_errors():
    assert _run(["query"])[0] == 1            # missing required 'text'
    assert _run(["bogus-command"])[0] == 1    # unknown subcommand
    assert _run([])[0] == 1                    # no command -> help on stderr, exit 1


def test_help_is_zero():
    code, out, _ = _run(["--help"])
    assert code == 0 and "memorydb" in out


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
