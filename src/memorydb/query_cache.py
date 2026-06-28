"""In-memory query-embedding cache, model-scoped (TD-011).

The query→vector mapping is a pure function of ``(embedding model, query text)`` — it does NOT depend on
the store — so caching it avoids the dominant per-query cost (a real model's forward pass / API call) on
repeated or paginated questions. The cache is keyed by ``sha256(query)`` (fixed 32-byte keys → a clean
fixed-stride binary dump; raw queries never hit disk), bounded (oldest-evicted), clearable and rewritable,
and bound to the embedding model (``reconcile`` clears it if handed to a different model/dim).

Persistence is an OPTIONAL, disposable binary dump (no pickle/JSON): a header (incl. a CRC32) + a tight
array of fixed-width little-endian records, model-validated and CRC-checked on load so a different model
or a corrupt file is never reused. The substrate's ``embeddings`` BLOBs remain the authoritative vectors
(TD-004); this file is a hot-query accelerator, safe to delete.
"""
from __future__ import annotations

import array
import binascii
import hashlib
import os
import struct
import sys
import tempfile
from typing import Optional, Sequence

_MAGIC = b"MQEC"        # MemoryDB Query Embedding Cache
_VERSION = 2
_HEADER = struct.Struct("<BIH")   # version u8, dim u32, model_len u16  (after the 4-byte magic)
_U32 = struct.Struct("<I")


def _pack_le(vec: Sequence[float]) -> bytes:
    """float32 little-endian, regardless of host byte order (so a dump is portable — re-review T11-8)."""
    a = array.array("f", vec)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _unpack_le(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    if sys.byteorder == "big":
        a.byteswap()
    return a


class QueryEmbeddingCache:
    """``sha256(query) -> embedding vector`` map, scoped to one embedding model. ``get``/``put`` hash the
    query internally; ``put`` is rewritable and bounded (oldest-evicted) and adopts the dim of the real
    embedding. ``reconcile`` binds the cache to an embedder's identity (clearing on a model/dim change).
    ``dump``/``load`` persist a compact, model- and CRC-validated little-endian binary file."""

    def __init__(self, model_id: str, dim: Optional[int] = None, max_entries: int = 512) -> None:
        self.model_id = str(model_id)
        self.dim = dim
        self.max_entries = max_entries
        self._map: dict = {}   # sha256 digest (bytes) -> list[float]

    @staticmethod
    def _key(query: str) -> bytes:
        # surrogatepass so the key is total over every Python str the embedder itself accepts (T11-7).
        return hashlib.sha256(query.encode("utf-8", "surrogatepass")).digest()

    def reconcile(self, model_id: str, dim: Optional[int] = None) -> None:
        """Bind to an embedder's identity. If the cache was tagged for a DIFFERENT model (or a known,
        different dim) — e.g. an injected/shared cache handed to another model — it is cleared so a
        cross-model/wrong-dim vector is never served (re-review T11-1). Then it adopts the new identity."""
        model_id = str(model_id)
        if self.model_id != model_id or (dim is not None and self.dim is not None and self.dim != dim):
            self.clear()
        self.model_id = model_id
        if dim is not None:
            self.dim = dim

    def get(self, query: str) -> Optional[list]:
        return self._map.get(self._key(query))

    def put(self, query: str, vector: Sequence[float]) -> None:
        vec = list(vector)
        if not vec:
            return                      # never cache an empty vector
        if self.dim is None:
            self.dim = len(vec)
        elif len(vec) != self.dim:
            # the real embedding's length is authoritative over a stale loaded/advertised dim: adopt it
            # and drop the now-invalid entries, rather than silently dropping every real put (T11-5/6).
            self.clear()
            self.dim = len(vec)
        self._map[self._key(query)] = vec
        if self.max_entries and len(self._map) > self.max_entries:
            self._map.pop(next(iter(self._map)), None)   # oldest-evicted (insertion order)

    def clear(self) -> None:
        self._map.clear()

    def __len__(self) -> int:
        return len(self._map)

    # --- persistence (optional, disposable binary dump) -------------------
    def dump(self, path: str) -> int:
        """Write the hashmap to a flat little-endian binary file (unique temp + atomic rename; the temp is
        cleaned up on any failure). Returns the record count; a no-op (0) when nothing is cached yet."""
        if self.dim is None:
            return 0
        model = self.model_id.encode("utf-8")
        if len(model) > 0xFFFF:
            raise ValueError("model_id too long to persist (> 65535 UTF-8 bytes)")
        body = bytearray()
        for key, vec in self._map.items():
            body += key                # 32-byte sha256 digest
            body += _pack_le(vec)      # dim * float32 LE
        crc = binascii.crc32(bytes(body)) & 0xFFFFFFFF
        header = (_MAGIC + _HEADER.pack(_VERSION, int(self.dim), len(model)) + model
                  + _U32.pack(len(self._map)) + _U32.pack(crc))
        # unique temp per writer in the SAME dir, so concurrent dumps to one shared file don't clobber a
        # single hard-coded {path}.tmp (re-review T11-2); atomic rename; cleanup on failure (T11-3).
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mqec-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(header)
                f.write(bytes(body))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(self._map)

    def load(self, path: str) -> int:
        """Warm from a dump, MERGING into the running cache (live entries are kept). Validated: a missing,
        malformed, wrong-model/dim, truncated, or CRC-mismatched file is IGNORED (returns 0; never a
        crash, never a cross-model/corrupt vector). Returns the number of records merged in."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        fixed = 4 + _HEADER.size
        if len(data) < fixed or data[:4] != _MAGIC:
            return 0
        try:
            version, dim, mlen = _HEADER.unpack_from(data, 4)
            off = fixed
            if version != _VERSION:
                return 0
            model = data[off:off + mlen].decode("utf-8")
            off += mlen
            (count,) = _U32.unpack_from(data, off)
            off += _U32.size
            (crc,) = _U32.unpack_from(data, off)
            off += _U32.size
        except (struct.error, UnicodeDecodeError):
            return 0
        if model != self.model_id or (self.dim is not None and dim != self.dim):
            return 0                                  # different model/dim -> never reuse
        if dim <= 0:
            return 0
        stride = 32 + 4 * dim
        if off + count * stride != len(data):         # exact-size check: truncated/over-long -> ignore
            return 0
        body = data[off:]
        if (binascii.crc32(body) & 0xFFFFFFFF) != crc:   # same-length corruption (bit flip) -> ignore
            return 0
        loaded: dict = {}
        for i in range(count):
            base = i * stride
            loaded[body[base:base + 32]] = list(_unpack_le(body[base + 32:base + stride]))
        if self.dim is None:
            self.dim = dim
        self._map.update(loaded)                      # MERGE — keep live in-memory entries (T11-4)
        while self.max_entries and len(self._map) > self.max_entries:
            self._map.pop(next(iter(self._map)), None)
        return len(loaded)
