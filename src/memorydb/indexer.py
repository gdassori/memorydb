"""Indexer & ingestion pipeline (indexer-ingestion-pipeline spec; TD-003/005/006).

Walks a directory, extracts (nodes, edges, pending) per file, upserts into the Store in two passes
(all nodes, then edges — so cross-file references resolve), resolves pending edges against the global
symbol table (C2), and re-embeds the dirty nodes. Incremental via a per-file sha256 on a `file` node;
deletions and changes drop a file's symbols by `attrs.file_uid` (NOT a file-node FK cascade — C5).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from typing import Optional

from pydantic import BaseModel, Field

from .models import Node

_DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".tox", "site-packages",
}


class IgnoreMatcher:
    def __init__(self, dirs: Optional[set] = None, max_bytes: int = 1_000_000) -> None:
        self.dirs = dirs or set(_DEFAULT_IGNORE_DIRS)
        self.max_bytes = max_bytes

    def skip_dir(self, name: str) -> bool:
        return name in self.dirs or name.startswith(".")

    def skip_file(self, abspath: str) -> bool:
        try:
            if os.path.getsize(abspath) > self.max_bytes:
                return True
            with open(abspath, "rb") as fh:
                return b"\x00" in fh.read(4096)  # crude binary sniff
        except OSError:
            return True


class IndexReport(BaseModel):
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_deleted: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    edges_unresolved: int = 0
    embedded: int = 0


class _Merged(BaseModel):
    nodes: list = Field(default_factory=list)
    edges: list = Field(default_factory=list)
    pending: list = Field(default_factory=list)
    lang: Optional[str] = None


class Indexer:
    def __init__(self, store, extractors, embedder=None, ignore: Optional[IgnoreMatcher] = None) -> None:
        self.store = store
        self.extractors = list(extractors)
        self.embedder = embedder
        self.ignore = ignore or IgnoreMatcher()

    def index(self, root: str, *, force: bool = False) -> IndexReport:
        rep = IndexReport()
        root = os.path.abspath(os.path.expanduser(root))  # so "~/src/repo" works as documented (PR1-4)
        for ex in self.extractors:            # keep relpaths consistent with the indexed root
            if hasattr(ex, "repo_root"):
                ex.repo_root = root

        disk = self._discover(root)
        existing = self._existing_files()
        rep.files_seen = len(disk)

        # Names added or removed this run — the callers referencing these by name must be re-resolved,
        # even if their own files didn't change (this is what rebuilds a cross-file edge after its
        # callee file is edited — R3L-1).
        affected_names: set = set()

        # The whole graph ingestion (deletions + both passes + pending resolution) runs in ONE
        # transaction and commits exactly once. A crash mid-run therefore rolls back cleanly and can
        # never leave a file's sha256 skip-token durable ahead of the edges it gates (data-integrity
        # MR-2). Same-connection reads in pass 2 see pass 1's uncommitted writes.
        try:
            # Deletions: a file node exists but the file is gone.
            for rel in set(existing) - set(disk):
                affected_names |= self._symbol_names_of(rel)
                self._delete_file(rel)
                rep.files_deleted += 1

            # Diff by content hash (force=True re-indexes everything — a recovery escape hatch). The
            # bytes read here for hashing are reused by the extractors (avoids re-reading; perf MR-15).
            changed = []
            for rel, path in disk.items():
                try:
                    with open(path, "rb") as fh:   # context manager: no leaked handle (MR-21)
                        data = fh.read()
                except OSError:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if not force and existing.get(rel, {}).get("sha256") == sha:
                    rep.files_skipped += 1
                    continue
                changed.append((rel, path, sha, data))

            # PASS 1 — upsert all nodes (so cross-file edge endpoints exist in pass 2).
            deferred = []
            for rel, path, sha, data in changed:
                affected_names |= self._symbol_names_of(rel)   # names this file is about to drop/replace
                self._delete_file(rel)            # drop any prior symbols/file node for this path
                merged = self._extract_all(path, data)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = None
                # mtime is the recency signal reused by the FILTER builder / hybrid ranker (C5); sha256
                # remains the authoritative change check.
                self.store.upsert_node(Node(uid=rel, type="file", name=os.path.basename(rel),
                                            attrs={"sha256": sha, "mtime": mtime, "lang": merged.lang}))
                for nd in merged.nodes:
                    self.store.upsert_node(nd)
                    affected_names.add(nd.name)   # names this file now defines
                    rep.nodes_upserted += 1
                deferred.append((rel, merged))
                rep.files_indexed += 1

            # PASS 2 — in-file precise edges (dst is already a uid) now; by-name edges are persisted to
            # pending_edges (C2) and resolved in one global pass below.
            changed_rels: set = set()
            for rel, merged in deferred:
                changed_rels.add(rel)
                for e in merged.edges:
                    if self._safe_edge(e.src, e.dst, e.relation, e.confidence, e.source):
                        rep.edges_upserted += 1
                    else:
                        rep.edges_unresolved += 1
                    # A precise CROSS-FILE edge is also recorded as a durable pending row (at its true
                    # confidence) so that editing the callee file — which cascade-deletes the edge while
                    # the unchanged caller is skipped — rebuilds it at >=0.97 instead of falling back to
                    # a coarse 0.6 pending (data-integrity MR-3).
                    self._persist_cross_file(e)
                for (src_uid, dst_name, relation, conf) in merged.pending:
                    self._persist_pending(src_uid, rel, dst_name, relation, conf)

            # Resolve every pending edge that could have changed this run: emitted by a (re)indexed
            # file, OR (in any unchanged file) targeting a name that just appeared/disappeared.
            up, un = self._resolve_pending(changed_rels, affected_names)
            rep.edges_upserted += up
            rep.edges_unresolved += un
            self.store.commit()
        except Exception:
            self.store.conn.rollback()
            raise

        # Embeddings (graph-aware, TD-006): only the dirty nodes. Outside the atomic graph unit — a
        # failed embed just leaves nodes dirty for the next refresh.
        if self.embedder is not None:
            from .embedding_pipeline import EmbeddingPipeline
            rep.embedded = EmbeddingPipeline(self.store, self.embedder).refresh().embedded
        return rep

    # --- helpers -----------------------------------------------------------
    def _discover(self, root: str) -> dict:
        out: dict = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not self.ignore.skip_dir(d)]
            for fn in filenames:
                ap = os.path.join(dirpath, fn)
                # Skip symlinked files: os.walk already blocks symlinked dirs, but a symlinked file
                # would otherwise be read through its target — escaping the indexed root (security I5).
                if os.path.islink(ap):
                    continue
                if not self._any_handles(ap) or self.ignore.skip_file(ap):
                    continue
                out[os.path.relpath(ap, root).replace(os.sep, "/")] = ap
        return out

    def _any_handles(self, path: str) -> bool:
        return any(getattr(ex, "handles", lambda p: True)(path) for ex in self.extractors)

    def _accepts_data(self, ex) -> bool:
        """Whether ``ex.extract`` takes a ``data`` argument (cached per extractor). Lets the indexer
        pass already-read bytes without breaking simpler extractors whose signature is ``extract(path)``."""
        cache = self.__dict__.setdefault("_data_ok", {})
        key = id(ex)
        if key not in cache:
            try:
                params = inspect.signature(ex.extract).parameters
                cache[key] = "data" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
            except (ValueError, TypeError):
                cache[key] = False
        return cache[key]

    def _extract_all(self, path: str, data=None) -> _Merged:
        m = _Merged()
        for ex in self.extractors:
            if not getattr(ex, "handles", lambda p: True)(path):
                continue
            try:
                # Reuse the bytes already read for hashing when the extractor accepts them (perf MR-15);
                # one extractor must never abort the whole run (MR-1).
                if data is not None and self._accepts_data(ex):
                    res = ex.extract(path, data=data)
                else:
                    res = ex.extract(path)
            except Exception:
                continue
            m.nodes += res.nodes
            m.edges += res.edges
            m.pending += res.pending
            if m.lang is None:
                m.lang = getattr(ex, "lang_of", lambda p: None)(path)
        # Multiple extractors (e.g. the coarse CodeAdapter + the precise PythonResolver) emit the same
        # symbols under the same uid scheme — dedupe by uid so nodes_upserted isn't double-counted and
        # we don't upsert a symbol twice. Edges intentionally are NOT deduped: they merge in the store
        # by MAX-confidence, so the precise edge supersedes the coarse one for the same (src,dst,rel).
        seen: set = set()
        deduped: list = []
        for nd in m.nodes:
            if nd.uid not in seen:
                seen.add(nd.uid)
                deduped.append(nd)
        m.nodes = deduped
        return m

    def _existing_files(self) -> dict:
        out: dict = {}
        for row in self.store.conn.execute("SELECT uid, attrs FROM nodes WHERE type = 'file'"):
            attrs = json.loads(row["attrs"]) if row["attrs"] else {}
            out[row["uid"]] = {"sha256": attrs.get("sha256")}
        return out

    def _delete_file(self, rel: str) -> None:
        # Dirty the surviving node on the far end of each edge this file's symbols touch — their
        # serialized neighborhood (TD-006) changes when the symbol disappears, else their embedding is
        # silently stale. Must run BEFORE the delete cascades those edges away (R3L-3).
        self.store.conn.execute(
            "UPDATE nodes SET embed_dirty = 1 WHERE id IN ("
            "  SELECT e.dst FROM edges e JOIN nodes s ON s.id = e.src WHERE s.file_uid = ? AND s.type != 'file' "
            "  UNION "
            "  SELECT e.src FROM edges e JOIN nodes d ON d.id = e.dst WHERE d.file_uid = ? AND d.type != 'file')",
            (rel, rel),
        )
        # Symbols carry attrs.file_uid; deleting them cascades their edges (FK). Then drop the file node.
        # `file_uid` is the indexed VIRTUAL generated column (migration 3) over attrs.$.file_uid, so this
        # is an indexed lookup, not a full json_extract scan (perf I8).
        self.store.conn.execute(
            "DELETE FROM nodes WHERE file_uid = ? AND type != 'file'", (rel,)
        )
        self.store.conn.execute("DELETE FROM nodes WHERE uid = ? AND type = 'file'", (rel,))
        # Drop this file's pending edges; they are re-emitted if/when the file is re-indexed (R3L-1).
        self.store.conn.execute("DELETE FROM pending_edges WHERE src_file = ?", (rel,))

    def _symbol_names_of(self, rel: str) -> set:
        rows = self.store.conn.execute(
            "SELECT DISTINCT name FROM nodes WHERE file_uid = ? AND type != 'file'", (rel,)
        ).fetchall()
        return {r[0] for r in rows}

    def _persist_cross_file(self, e) -> None:
        """Record a precise CROSS-FILE direct edge as a durable pending row so it survives a callee
        re-index at its true confidence (MR-3). In-file edges are skipped — they are re-emitted
        whenever their own file changes."""
        src_file = e.src.split("::", 1)[0]
        if "::" not in e.dst or e.dst.split("::", 1)[0] == src_file:
            return
        dst_name = e.dst.split("::", 1)[1].split(".")[-1]   # node `name` = last qualname component
        self._persist_pending(e.src, src_file, dst_name, e.relation, e.confidence)

    def _persist_pending(self, src_uid, src_file, dst_name, relation, confidence) -> None:
        self.store.conn.execute(
            "INSERT INTO pending_edges(src_uid, src_file, dst_name, relation, confidence) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(src_uid, dst_name, relation) DO UPDATE SET "
            "  src_file = excluded.src_file, "
            "  confidence = MAX(pending_edges.confidence, excluded.confidence)",
            (src_uid, src_file, dst_name, relation, confidence),
        )

    def _resolve_pending(self, changed_rels: set, affected_names: set):
        """Resolve the candidate pending rows by name; returns (upserted, unresolved). Candidates are
        rows from a (re)indexed file OR rows whose target name changed this run. Unique name match →
        edge (MAX-confidence upsert); ambiguous/unknown → left pending for a future run."""
        clauses, params = [], []
        if changed_rels:
            clauses.append("src_file IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sorted(changed_rels)))
        if affected_names:
            clauses.append("dst_name IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sorted(affected_names)))
        if not clauses:
            return 0, 0
        rows = self.store.conn.execute(
            "SELECT src_uid, dst_name, relation, confidence FROM pending_edges WHERE "
            + " OR ".join(clauses),
            params,
        ).fetchall()
        up = un = 0
        name_cache: dict = {}   # memoize name->uids: many pending rows share a target name (perf MR-14)
        for r in rows:
            name = r["dst_name"]
            if name not in name_cache:
                name_cache[name] = self._resolve_name(name)
            targets = name_cache[name]
            if len(targets) == 1 and self._safe_edge(
                r["src_uid"], targets[0], r["relation"], r["confidence"], "treesitter"
            ):
                up += 1
            else:
                un += 1
        return up, un

    def _resolve_name(self, name: str) -> list:
        rows = self.store.conn.execute(
            "SELECT uid FROM nodes WHERE name = ? AND type != 'file'", (name,)
        ).fetchall()
        return [r["uid"] for r in rows]

    def _safe_edge(self, src_uid, dst_uid, relation, confidence, source) -> bool:
        try:
            self.store.upsert_edge(src_uid, dst_uid, relation, confidence=confidence, source=source)
            return True
        except KeyError:
            return False
