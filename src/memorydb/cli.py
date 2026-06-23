"""Command-line interface — ``memorydb`` (cli spec; TD-002/004).

A thin stdlib ``argparse`` CLI over the :class:`~memorydb.api.MemoryDB` facade: index a repo, query
it, inspect status. Zero extra dependencies — all real work lives in the facade; this module only
parses args and renders output. Exit codes: 0 ok, 1 usage error, 2 runtime error.

Note: unlike ``MemoryDB.open()`` (which defaults to ``:memory:``), the CLI **persists** by default to
``./memorydb.sqlite`` — a shell invocation should keep its index. ``query`` / ``status`` on an empty or
missing DB print "no data, run `index`" rather than erroring.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import warnings
from typing import Optional

from .api import ContextResult, MemoryDB
from .embedders import HashingEmbedder

_DEFAULT_DB = "./memorydb.sqlite"


# --- argparse plumbing -----------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit 1 (the spec's contract), not argparse's default 2."""

    def error(self, message: str):  # pragma: no cover - exercised via main()
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _build_parser() -> _Parser:
    p = _Parser(prog="memorydb", description="Embedded knowledge substrate for local LLMs.")
    p.add_argument("--db", default=_DEFAULT_DB,
                   help=f"SQLite path (default: {_DEFAULT_DB}; the CLI persists, unlike the in-memory API)")
    p.add_argument("--embedder", metavar="module:attr",
                   help="dotted path to an Embedder instance/factory (default: HashingEmbedder, not "
                        "semantic-quality)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("index", help="walk a path and ingest symbols + edges")
    s.add_argument("path")
    s.add_argument("--no-embed", action="store_true", help="ingest the graph but defer embedding")
    s.add_argument("--force", action="store_true", help="re-index every file (ignore the sha256 skip)")
    s.set_defaults(func=_cmd_index)

    s = sub.add_parser("query", help="route a question by intent (LOCATE/EXPLAIN)")
    s.add_argument("text")
    s.add_argument("-k", type=int, default=5, help="vector seeds for EXPLAIN (default 5)")
    s.add_argument("--depth", type=int, default=2, help="graph hops for EXPLAIN (default 2)")
    s.add_argument("--context", action="store_true", help="pack the result into LLM-ready context")
    s.add_argument("--budget", type=int, default=2000, help="token budget for --context (default 2000)")
    s.add_argument("--json", action="store_true", help="emit JSON instead of text")
    s.set_defaults(func=_cmd_query)

    s = sub.add_parser("locate", help="exact references to a symbol")
    s.add_argument("symbol")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_locate)

    s = sub.add_parser("explain", help="force the EXPLAIN path for a question")
    s.add_argument("text")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_explain)

    s = sub.add_parser("status", help="node/edge/embedding counts + schema version")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)

    s = sub.add_parser("reembed", help="re-embed stale (or all) nodes")
    s.add_argument("--full", action="store_true", help="re-embed everything, not just dirty nodes")
    s.set_defaults(func=_cmd_reembed)
    return p


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse: usage -> 1 (our _Parser), -h/--help -> 0
        return int(e.code or 0)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 1
    try:
        return args.func(args)
    except BrokenPipeError:  # e.g. piping into `head`
        return 0
    except Exception as e:  # runtime error -> 2, with a clear message (no traceback)
        print(f"error: {e}", file=sys.stderr)
        return 2


# --- shared helpers --------------------------------------------------------
def _resolve_embedder(spec: Optional[str]):
    """Return ``(embedder, used_default)``. ``spec`` is a ``module:attr`` (or ``module.attr``) path to
    an Embedder instance or zero-arg factory; ``None`` falls back to the offline HashingEmbedder."""
    if not spec:
        return HashingEmbedder(), True
    mod_name, _, attr = spec.replace(":", ".").rpartition(".")
    if not mod_name:
        raise ValueError(f"--embedder must be 'module:attr', got {spec!r}")
    obj = getattr(importlib.import_module(mod_name), attr)
    return (obj() if callable(obj) else obj), False


def _open(args) -> MemoryDB:
    embedder, used_default = _resolve_embedder(getattr(args, "embedder", None))
    if used_default:
        print("warning: using the default HashingEmbedder (offline, not semantic-quality); "
              "pass --embedder for production retrieval.", file=sys.stderr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the facade's own default-embedder warning; we printed ours
        return MemoryDB.open(args.db, embedder=embedder)


def _node_count(db: MemoryDB) -> int:
    return db.store.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


def _no_data(db: MemoryDB, as_json: bool = False) -> bool:
    if _node_count(db) == 0:
        print("no data — run `memorydb index <path>` first.", file=sys.stderr)
        if as_json:
            print("{}")   # emit a valid (empty) JSON document so a --json consumer doesn't choke (R6-15)
        return True
    return False


def _path_of(uid: str) -> str:
    return uid.split("::", 1)[0] if "::" in uid else uid


# --- command handlers ------------------------------------------------------
def _cmd_index(args) -> int:
    db = _open(args)
    try:
        if not os.path.exists(args.path):
            print(f"error: path not found: {args.path}", file=sys.stderr)
            return 2
        rep = db.index(args.path, embed=not args.no_embed, force=args.force)
        print(f"indexed {rep.files_indexed} files "
              f"({rep.files_skipped} unchanged, {rep.files_deleted} removed) · "
              f"{rep.nodes_upserted} symbols · {rep.edges_upserted} edges "
              f"({rep.edges_unresolved} unresolved) · embedded {rep.embedded}")
        return 0
    finally:
        db.close()


def _cmd_query(args) -> int:
    db = _open(args)
    try:
        if _no_data(db, args.json):
            return 0
        if args.context:
            ctx = db.ask(args.text, k=args.k, depth=args.depth, as_context=True, budget_tokens=args.budget)
            _render_context(ctx, args.json)
            return 0
        result = db.ask(args.text, k=args.k, depth=args.depth)
        _render_result(result, args.json)
        return 0
    finally:
        db.close()


def _cmd_locate(args) -> int:
    db = _open(args)
    try:
        if _no_data(db, args.json):
            return 0
        refs = db.locate(args.symbol)
        if args.json:
            print(json.dumps({"symbol": args.symbol, "references": refs}, indent=2))
        else:
            print(f"LOCATE {args.symbol}")
            for r in refs:
                print(f"  {r['src_name']}  {r['relation']}  (conf {r['confidence']:.2f})  "
                      f"{_path_of(r['src_uid'])}")
            if not refs:
                print("  (no references)")
        return 0
    finally:
        db.close()


def _cmd_explain(args) -> int:
    db = _open(args)
    try:
        if _no_data(db, args.json):
            return 0
        _render_result(db.explain(args.text, k=args.k, depth=args.depth), args.json)
        return 0
    finally:
        db.close()


def _cmd_status(args) -> int:
    db = _open(args)
    try:
        conn = db.store.conn
        info = {
            "db": args.db,
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "nodes": _node_count(db),
            "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
            "dirty": conn.execute("SELECT COUNT(*) FROM nodes WHERE embed_dirty = 1").fetchone()[0],
            "embed_model": db.store.get_meta("embed_model"),
            "embed_dim": db.store.get_meta("embed_dim"),
        }
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(f"db: {info['db']}  (schema v{info['schema_version']})")
            print(f"nodes: {info['nodes']}  edges: {info['edges']}  "
                  f"embeddings: {info['embeddings']}  dirty: {info['dirty']}")
            print(f"embedder: {info['embed_model']}  dim: {info['embed_dim']}")
        return 0
    finally:
        db.close()


def _cmd_reembed(args) -> int:
    db = _open(args)
    try:
        rep = db.refresh_embeddings(full=args.full)
        print(f"embedded {rep.embedded} nodes ({rep.failed} failed, {rep.batches} batches)")
        return 0
    finally:
        db.close()


# --- renderers -------------------------------------------------------------
def _render_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    intent = result.get("intent")
    if intent == "LOCATE":
        print(f"LOCATE {result.get('symbol') or '?'}"
              + ("  (ambiguous)" if result.get("ambiguous") else ""))
        for r in result.get("references", []):
            print(f"  {r['src_name']}  {r['relation']}  (conf {r['confidence']:.2f})  "
                  f"{_path_of(r['src_uid'])}")
        if not result.get("references"):
            print("  (no references)")
    elif intent == "EXPLAIN":
        nodes = result.get("nodes", [])
        print(f"EXPLAIN  ({len(nodes)} nodes, {len(result.get('edges', []))} edges)")
        for n in nodes:
            attrs = n.get("attrs") or {}
            loc = f":{attrs['start_line']}" if attrs.get("start_line") else ""
            print(f"  {n['name']}  ({n['type']})  {_path_of(n['uid'])}{loc}")
    else:
        print(json.dumps(result, indent=2, default=str))


def _render_context(ctx: ContextResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "intent": ctx.intent, "uids": ctx.uids, "used_tokens": ctx.used_tokens,
            "budget_tokens": ctx.budget_tokens, "truncated": ctx.truncated, "text": ctx.text,
        }, indent=2))
        return
    print(f"# context ({ctx.intent}) · {ctx.used_tokens}/{ctx.budget_tokens} tokens"
          + ("  [truncated]" if ctx.truncated else ""))
    print(ctx.text)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
