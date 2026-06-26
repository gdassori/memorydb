"""In-memory query-embedding cache, model-scoped (TD-011).

The query→vector mapping is a pure function of ``(embedding model, query text)`` — it does NOT depend on
the store — so caching it avoids the dominant per-query cost (a real model's forward pass / API call) on
repeated or paginated questions. The cache is keyed by ``sha256(query)`` (fixed 32-byte keys → a clean
fixed-stride binary dump; raw queries never hit disk), bounded (oldest-evicted), clearable and rewritable.

Persistence is an OPTIONAL, disposable binary dump (no pickle/JSON): a header + a tight array of fixed-width
records, model-validated on load so a different model is never reused. The substrate's ``embeddings`` BLOBs
remain the authoritative vectors (TD-004); this file is a hot-query accelerator, safe to delete.
"""
from __future__ import annotations

import hashlib
import os
import struct
from typing import Optional, Sequence

from .vector import pack, unpack

_MAGIC = b"MQEC"        # MemoryDB Query Embedding Cache
_VERSION = 1
_HEADER = struct.Struct("<BIH")   # version u8, dim u32, model_len u16  (after the 4-byte magic)
_U32 = struct.Struct("<I")


class QueryEmbeddingCache:
    """``sha256(query) -> embedding vector`` map, scoped to one embedding model. ``get``/``put`` hash the
    query internally; ``put`` is rewritable and bounded (oldest-evicted). ``dump``/``load`` persist a
    compact, model-validated binary file."""

    def __init__(self, model_id: str, dim: Optional[int] = None, max_entries: int = 512) -> None:
        self.model_id = str(model_id)
        self.dim = dim
        self.max_entries = max_entries
        self._map: dict = {}   # sha256 digest (bytes) -> list[float]

    @staticmethod
    def _key(query: str) -> bytes:
        return hashlib.sha256(query.encode("utf-8")).digest()

    def get(self, query: str) -> Optional[list]:
        return self._map.get(self._key(query))

    def put(self, query: str, vector: Sequence[float]) -> None:
        vec = list(vector)
        if self.dim is None:
            self.dim = len(vec)
        if len(vec) != self.dim:   # a wrong-dim vector means a model mismatch -> ignore, don't poison
            return
        self._map[self._key(query)] = vec
        if self.max_entries and len(self._map) > self.max_entries:
            self._map.pop(next(iter(self._map)), None)   # oldest-evicted (insertion order)

    def clear(self) -> None:
        self._map.clear()

    def __len__(self) -> int:
        return len(self._map)

    # --- persistence (optional, disposable binary dump) -------------------
    def dump(self, path: str) -> int:
        """Write the hashmap to a flat binary file (atomic temp+rename). Returns the record count. A
        no-op (returns 0) when the dim is still unknown (nothing cached yet)."""
        if self.dim is None:
            return 0
        model = self.model_id.encode("utf-8")
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(_MAGIC)
            f.write(_HEADER.pack(_VERSION, int(self.dim), len(model)))
            f.write(model)
            f.write(_U32.pack(len(self._map)))
            for key, vec in self._map.items():
                f.write(key)         # 32-byte sha256 digest
                f.write(pack(vec))   # dim * float32
        os.replace(tmp, path)        # atomic: a crash mid-write never corrupts a good cache
        return len(self._map)

    def load(self, path: str) -> int:
        """Warm from a dump. Model-validated: if the file is missing, malformed, the wrong model/dim, or
        truncated, it is IGNORED (the cache is left as-is, never a crash, never a cross-model vector).
        Returns the number of records loaded (0 on any reject)."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        if len(data) < 4 + _HEADER.size + _U32.size or data[:4] != _MAGIC:
            return 0
        try:
            version, dim, mlen = _HEADER.unpack_from(data, 4)
            off = 4 + _HEADER.size
            if version != _VERSION:
                return 0
            model = data[off:off + mlen].decode("utf-8")
            off += mlen
            (count,) = _U32.unpack_from(data, off)
            off += _U32.size
        except (struct.error, UnicodeDecodeError):
            return 0
        if model != self.model_id or (self.dim is not None and dim != self.dim):
            return 0                                  # different model/dim -> never reuse
        stride = 32 + 4 * dim
        if dim <= 0 or off + count * stride != len(data):   # exact-size check: truncated/corrupt -> ignore
            return 0
        loaded: dict = {}
        for _ in range(count):
            loaded[data[off:off + 32]] = list(unpack(data[off + 32:off + stride]))
            off += stride
        self._map = loaded
        self.dim = dim
        while self.max_entries and len(self._map) > self.max_entries:
            self._map.pop(next(iter(self._map)), None)
        return len(self._map)
