"""Indexer & ingestion pipeline (indexer-ingestion-pipeline spec; TD-003/005/006).

Walks a directory, extracts (nodes, edges, pending) per file, upserts into the Store in two passes
(all nodes, then edges — so cross-file references resolve), resolves pending edges against the global
symbol table (C2), and re-embeds the dirty nodes. Incremental via a per-file sha256 on a `file` node;
deletions and changes drop a file's symbols by `attrs.file_uid` (NOT a file-node FK cascade — C5).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional

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


@dataclass
class IndexReport:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_deleted: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    edges_unresolved: int = 0
    embedded: int = 0


@dataclass
class _Merged:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    lang: Optional[str] = None


class Indexer:
    def __init__(self, store, extractors, embedder=None, ignore: Optional[IgnoreMatcher] = None) -> None:
        self.store = store
        self.extractors = list(extractors)
        self.embedder = embedder
        self.ignore = ignore or IgnoreMatcher()

    def index(self, root: str) -> IndexReport:
        rep = IndexReport()
        root = os.path.abspath(root)
        for ex in self.extractors:            # keep relpaths consistent with the indexed root
            if hasattr(ex, "repo_root"):
                ex.repo_root = root

        disk = self._discover(root)
        existing = self._existing_files()
        rep.files_seen = len(disk)

        # Deletions: a file node exists but the file is gone.
        for rel in set(existing) - set(disk):
            self._delete_file(rel)
            rep.files_deleted += 1

        # Diff by content hash.
        changed = []
        for rel, path in disk.items():
            try:
                data = open(path, "rb").read()
            except OSError:
                continue
            sha = hashlib.sha256(data).hexdigest()
            if existing.get(rel, {}).get("sha256") == sha:
                rep.files_skipped += 1
                continue
            changed.append((rel, path, sha))

        # PASS 1 — upsert all nodes (so cross-file edge endpoints exist in pass 2).
        deferred = []
        for rel, path, sha in changed:
            self._delete_file(rel)            # drop any prior symbols/file node for this path
            merged = self._extract_all(path)
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
                rep.nodes_upserted += 1
            deferred.append((rel, merged))
            rep.files_indexed += 1
        self.store.commit()

        # PASS 2 — edges (in-file, dst is a uid) + pending (by-name, resolved globally, C2).
        for rel, merged in deferred:
            for e in merged.edges:
                if self._safe_edge(e.src, e.dst, e.relation, e.confidence, e.source):
                    rep.edges_upserted += 1
                else:
                    rep.edges_unresolved += 1
            for (src_uid, dst_name, relation, conf) in merged.pending:
                targets = self._resolve_name(dst_name)
                if len(targets) == 1 and self._safe_edge(src_uid, targets[0], relation, conf, "treesitter"):
                    rep.edges_upserted += 1
                else:
                    rep.edges_unresolved += 1   # unknown or ambiguous name
        self.store.commit()

        # Embeddings (graph-aware, TD-006): only the dirty nodes.
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

    def _extract_all(self, path: str) -> _Merged:
        m = _Merged()
        for ex in self.extractors:
            if not getattr(ex, "handles", lambda p: True)(path):
                continue
            res = ex.extract(path)
            m.nodes += res.nodes
            m.edges += res.edges
            m.pending += res.pending
            if m.lang is None:
                m.lang = getattr(ex, "lang_of", lambda p: None)(path)
        return m

    def _existing_files(self) -> dict:
        out: dict = {}
        for row in self.store.conn.execute("SELECT uid, attrs FROM nodes WHERE type = 'file'"):
            attrs = json.loads(row["attrs"]) if row["attrs"] else {}
            out[row["uid"]] = {"sha256": attrs.get("sha256")}
        return out

    def _delete_file(self, rel: str) -> None:
        # Symbols carry attrs.file_uid; deleting them cascades their edges (FK). Then drop the file node.
        # `file_uid` is the indexed VIRTUAL generated column (migration 3) over attrs.$.file_uid, so this
        # is an indexed lookup, not a full json_extract scan (perf I8).
        self.store.conn.execute(
            "DELETE FROM nodes WHERE file_uid = ? AND type != 'file'", (rel,)
        )
        self.store.conn.execute("DELETE FROM nodes WHERE uid = ? AND type = 'file'", (rel,))

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
